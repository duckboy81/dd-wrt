// SPDX-License-Identifier: GPL-2.0-or-later
/*
 *   Copyright (C) 2016 Namjae Jeon <linkinjeon@kernel.org>
 *   Copyright (C) 2018 Samsung Electronics Co., Ltd.
 */

#include <linux/freezer.h>
#include <linux/etherdevice.h>
#include <linux/moduleparam.h>

#include "smb_common.h"
#include "server.h"
#include "auth.h"
#include "buffer_pool.h"
#include "connection.h"
#include "transport_tcp.h"

static uint ksmbd_connection_limit;
module_param_named(connlimit, ksmbd_connection_limit, uint, 0644);
MODULE_PARM_DESC(connlimit, "Maximum connection limit permitted per interface");

#define IFACE_STATE_DOWN		BIT(0)
#define IFACE_STATE_CONFIGURED		BIT(1)

struct interface {
	struct task_struct	*ksmbd_kthread;
	struct socket		*ksmbd_socket;
	struct list_head	entry;
	char			*name;
	struct mutex		sock_release_lock;
	int			state;
};

static LIST_HEAD(iface_list);

static int bind_additional_ifaces;

struct tcp_transport {
	struct ksmbd_transport		transport;
	struct socket			*sock;
	struct kvec			*iov;
	unsigned int			nr_iov;
};

static struct ksmbd_transport_ops ksmbd_tcp_transport_ops;

static void tcp_stop_kthread(struct task_struct *kthread);
static struct interface *alloc_iface(char *ifname);

#define KSMBD_TRANS(t)	(&(t)->transport)
#define TCP_TRANS(t)	((struct tcp_transport *)container_of(t, \
				struct tcp_transport, transport))

static inline void ksmbd_tcp_nodelay(struct socket *sock)
{
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 8, 0)
	int val = 1;

	kernel_setsockopt(sock, SOL_TCP, TCP_NODELAY,
		(char *)&val, sizeof(val));
#else
	tcp_sock_set_nodelay(sock->sk);
#endif
}

static inline void ksmbd_tcp_reuseaddr(struct socket *sock)
{
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 8, 0)
	int val = 1;

	kernel_setsockopt(sock, SOL_SOCKET, SO_REUSEADDR,
		(char *)&val, sizeof(val));
#else
	sock_set_reuseaddr(sock->sk);
#endif
}

static inline void ksmbd_tcp_rcv_timeout(struct socket *sock, s64 secs)
{
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 8, 0)
#if LINUX_VERSION_CODE <= KERNEL_VERSION(4, 18, 0)
	struct timeval tv = { .tv_sec = secs, .tv_usec = 0 };
#else
	struct __kernel_old_timeval tv = { .tv_sec = secs, .tv_usec = 0 };
#endif

#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 1, 0)
	kernel_setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO_OLD, (char *)&tv,
			  sizeof(tv));
#else
	kernel_setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, (char *)&tv,
			  sizeof(tv));
#endif
#else
	lock_sock(sock->sk);
	if (secs && secs < MAX_SCHEDULE_TIMEOUT / HZ - 1)
		sock->sk->sk_rcvtimeo = secs * HZ;
	else
		sock->sk->sk_rcvtimeo = MAX_SCHEDULE_TIMEOUT;
	release_sock(sock->sk);
#endif
}

static inline void ksmbd_tcp_snd_timeout(struct socket *sock, s64 secs)
{
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 8, 0)
#if LINUX_VERSION_CODE <= KERNEL_VERSION(4, 18, 0)
	struct timeval tv = { .tv_sec = secs, .tv_usec = 0 };
#else
	struct __kernel_old_timeval tv = { .tv_sec = secs, .tv_usec = 0 };
#endif

#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 1, 0)
	kernel_setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO_OLD, (char *)&tv,
			  sizeof(tv));
#else
	kernel_setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, (char *)&tv,
			  sizeof(tv));
#endif
#else
	sock_set_sndtimeo(sock->sk, secs);
#endif
}

static struct tcp_transport *alloc_transport(struct interface *iface, struct socket *client_sk)
{
	struct tcp_transport *t;
	struct ksmbd_conn *conn;

	t = kzalloc(sizeof(*t), GFP_KERNEL);
	if (!t)
		return NULL;
	t->sock = client_sk;

	conn = ksmbd_conn_alloc();
	if (!conn) {
		kfree(t);
		return NULL;
	}

	conn->transport = KSMBD_TRANS(t);
	KSMBD_TRANS(t)->conn = conn;
	KSMBD_TRANS(t)->ops = &ksmbd_tcp_transport_ops;
	return t;
}

static void free_transport(struct tcp_transport *t)
{
	kernel_sock_shutdown(t->sock, SHUT_RDWR);
	sock_release(t->sock);
	t->sock = NULL;

	ksmbd_conn_free(KSMBD_TRANS(t)->conn);
	kfree(t->iov);
	kfree(t);
}

/**
 * kvec_array_init() - initialize a IO vector segment
 * @new:	IO vector to be initialized
 * @iov:	base IO vector
 * @nr_segs:	number of segments in base iov
 * @bytes:	total iovec length so far for read
 *
 * Return:	Number of IO segments
 */
static unsigned int kvec_array_init(struct kvec *new, struct kvec *iov,
		unsigned int nr_segs, size_t bytes)
{
	size_t base = 0;

	while (bytes || !iov->iov_len) {
		int copy = min(bytes, iov->iov_len);

		bytes -= copy;
		base += copy;
		if (iov->iov_len == base) {
			iov++;
			nr_segs--;
			base = 0;
		}
	}

	memcpy(new, iov, sizeof(*iov) * nr_segs);
	new->iov_base += base;
	new->iov_len -= base;
	return nr_segs;
}

/**
 * get_conn_iovec() - get connection iovec for reading from socket
 * @t:		TCP transport instance
 * @nr_segs:	number of segments in iov
 *
 * Return:	return existing or newly allocate iovec
 */
static struct kvec *get_conn_iovec(struct tcp_transport *t, unsigned int nr_segs)
{
	struct kvec *new_iov;

	if (t->iov && nr_segs <= t->nr_iov)
		return t->iov;

	/* not big enough -- allocate a new one and release the old */
	new_iov = kmalloc_array(nr_segs, sizeof(*new_iov), GFP_KERNEL);
	if (new_iov) {
		kfree(t->iov);
		t->iov = new_iov;
		t->nr_iov = nr_segs;
	}
	return new_iov;
}

static unsigned short ksmbd_tcp_get_port(const struct sockaddr *sa)
{
	switch (sa->sa_family) {
	case AF_INET:
		return ntohs(((struct sockaddr_in *)sa)->sin_port);
	case AF_INET6:
		return ntohs(((struct sockaddr_in6 *)sa)->sin6_port);
	}
	return 0;
}

/**
 * ksmbd_tcp_new_connection() - create a new tcp session on mount
 * @client_sk:	socket associated with new connection
 *
 * whenever a new connection is requested, create a conn thread
 * (session thread) to handle new incoming smb requests from the connection
 *
 * Return:	0 on success, otherwise error
 */
static int ksmbd_tcp_new_connection(struct interface *iface, struct socket *client_sk)
{
	struct sockaddr *csin;
	int rc = 0;
	struct tcp_transport *t;

	t = alloc_transport(iface, client_sk);
	if (!t) {
		printk(KERN_ERR "Out of memory in %s:%d\n", __func__,__LINE__);
		return -ENOMEM;
	}

	csin = KSMBD_TCP_PEER_SOCKADDR(KSMBD_TRANS(t)->conn);

#if LINUX_VERSION_CODE <= KERNEL_VERSION(4, 16, 0)
	if (kernel_getpeername(client_sk, csin, &rc) < 0) {
		ksmbd_err("client ip resolution failed\n");
		rc = -EINVAL;
		goto out_error;
	}
	rc = 0;
#else
	if (kernel_getpeername(client_sk, csin) < 0) {
		ksmbd_err("client ip resolution failed\n");
		rc = -EINVAL;
		goto out_error;
	}
#endif
	KSMBD_TRANS(t)->handler = kthread_run(ksmbd_conn_handler_loop,
					KSMBD_TRANS(t)->conn,
					"ksmbd:%u", ksmbd_tcp_get_port(csin));
	if (IS_ERR(KSMBD_TRANS(t)->handler)) {
		ksmbd_err("cannot start conn thread\n");
		rc = PTR_ERR(KSMBD_TRANS(t)->handler);
		free_transport(t);
	}
	return rc;

out_error:
	free_transport(t);
	return rc;
}

/**
 * ksmbd_kthread_fn() - listen to new SMB connections and callback server
 * @p:		arguments to forker thread
 *
 * Return:	Returns a task_struct or ERR_PTR
 */
static int ksmbd_kthread_fn(void *p)
{
	struct socket *client_sk = NULL;
	struct interface *iface = (struct interface *)p;
	int ret;

	while (!kthread_should_stop()) {
		mutex_lock(&iface->sock_release_lock);
		if (!iface->ksmbd_socket) {
			mutex_unlock(&iface->sock_release_lock);
			break;
		}
		ret = kernel_accept(iface->ksmbd_socket, &client_sk,
				O_NONBLOCK);
		mutex_unlock(&iface->sock_release_lock);
		if (ret) {
			if (ret == -EAGAIN) {
				/* check for new connections every 100 msecs */
				schedule_timeout_interruptible(HZ / 10);
			}
			continue;
		}

		ksmbd_debug(CONN, "connect success: accepted new connection\n");
		client_sk->sk->sk_rcvtimeo = KSMBD_TCP_RECV_TIMEOUT;
		client_sk->sk->sk_sndtimeo = KSMBD_TCP_SEND_TIMEOUT;

		ksmbd_tcp_new_connection(iface, client_sk);
	}

	ksmbd_debug(CONN, "releasing socket\n");
	return 0;
}

/**
 * ksmbd_tcp_run_kthread() - start forker thread
 * @iface: pointer to struct interface
 *
 * start forker thread(ksmbd/0) at module init time to listen
 * on port 445 for new SMB connection requests. It creates per connection
 * server threads(ksmbd/x)
 *
 * Return:	0 on success or error number
 */
static int ksmbd_tcp_run_kthread(struct interface *iface)
{
	int rc;
	struct task_struct *kthread;

	kthread = kthread_run(ksmbd_kthread_fn, (void *)iface,
		"ksmbd-%s", iface->name);
	if (IS_ERR(kthread)) {
		rc = PTR_ERR(kthread);
		return rc;
	}
	iface->ksmbd_kthread = kthread;

	return 0;
}

/**
 * ksmbd_tcp_readv() - read data from socket in given iovec
 * @t:		TCP transport instance
 * @iov_orig:	base IO vector
 * @nr_segs:	number of segments in base iov
 * @to_read:	number of bytes to read from socket
 *
 * Return:	on success return number of bytes read from socket,
 *		otherwise return error number
 */
static int ksmbd_tcp_readv(struct tcp_transport *t, struct kvec *iov_orig,
		unsigned int nr_segs, unsigned int to_read)
{
	int length = 0;
	int total_read;
	unsigned int segs;
	struct msghdr ksmbd_msg;
	struct kvec *iov;
	struct ksmbd_conn *conn = KSMBD_TRANS(t)->conn;

	iov = get_conn_iovec(t, nr_segs);
	if (!iov) {
		printk(KERN_ERR "Out of memory in %s:%d\n", __func__,__LINE__);
		return -ENOMEM;
	}

	ksmbd_msg.msg_control = NULL;
	ksmbd_msg.msg_controllen = 0;

	for (total_read = 0; to_read; total_read += length, to_read -= length) {
		try_to_freeze();

		if (!ksmbd_conn_alive(conn)) {
			total_read = -ESHUTDOWN;
			break;
		}
		segs = kvec_array_init(iov, iov_orig, nr_segs, total_read);

		length = kernel_recvmsg(t->sock, &ksmbd_msg,
					iov, segs, to_read, 0);

		if (length == -EINTR) {
			total_read = -ESHUTDOWN;
			break;
		} else if (conn->status == KSMBD_SESS_NEED_RECONNECT) {
			total_read = -EAGAIN;
			break;
		} else if (length == -ERESTARTSYS || length == -EAGAIN) {
			usleep_range(1000, 2000);
			length = 0;
			continue;
		} else if (length <= 0) {
			total_read = -EAGAIN;
			break;
		}
	}
	return total_read;
}

/**
 * ksmbd_tcp_read() - read data from socket in given buffer
 * @t:		TCP transport instance
 * @buf:	buffer to store read data from socket
 * @to_read:	number of bytes to read from socket
 *
 * Return:	on success return number of bytes read from socket,
 *		otherwise return error number
 */
static int ksmbd_tcp_read(struct ksmbd_transport *t, char *buf, unsigned int to_read)
{
	struct kvec iov;

	iov.iov_base = buf;
	iov.iov_len = to_read;

	return ksmbd_tcp_readv(TCP_TRANS(t), &iov, 1, to_read);
}

static int ksmbd_tcp_writev(struct ksmbd_transport *t, struct kvec *iov,
		int nvecs, int size, bool need_invalidate, unsigned int remote_key)

{
	struct msghdr smb_msg = {.msg_flags = MSG_NOSIGNAL};

	return kernel_sendmsg(TCP_TRANS(t)->sock, &smb_msg, iov, nvecs, size);
}

static void ksmbd_tcp_disconnect(struct ksmbd_transport *t)
{
	free_transport(TCP_TRANS(t));
}

static void tcp_destroy_socket(struct socket *ksmbd_socket)
{
	int ret;

	if (!ksmbd_socket)
		return;

	/* set zero to timeout */
	ksmbd_tcp_rcv_timeout(ksmbd_socket, 0);
	ksmbd_tcp_snd_timeout(ksmbd_socket, 0);

	ret = kernel_sock_shutdown(ksmbd_socket, SHUT_RDWR);
	if (ret)
		ksmbd_err("Failed to shutdown socket: %d\n", ret);
	else
		sock_release(ksmbd_socket);
}

static const struct in6_addr my_in6addr_any = IN6ADDR_ANY_INIT;

/**
 * create_socket - create socket for ksmbd/0
 *
 * Return:	Returns a task_struct or ERR_PTR
 */
static int create_socket(struct interface *iface)
{
	int ret;
	struct sockaddr_in6 sin6;
	struct sockaddr_in sin;
	struct socket *ksmbd_socket;
	bool ipv4 = false;

	ret = sock_create(PF_INET6, SOCK_STREAM, IPPROTO_TCP, &ksmbd_socket);
	if (ret) {
		ksmbd_debug(ALL, "Can't create socket for ipv6, try ipv4: %d\n", ret);
		ret = sock_create(PF_INET, SOCK_STREAM, IPPROTO_TCP,
				&ksmbd_socket);
		if (ret) {
			ksmbd_err("Can't create socket for ipv4: %d\n", ret);
			goto out_error;
		}

		sin.sin_family = PF_INET;
		sin.sin_addr.s_addr = htonl(INADDR_ANY);
		sin.sin_port = htons(server_conf.tcp_port);
		ipv4 = true;
	} else {
		sin6.sin6_family = PF_INET6;
		sin6.sin6_addr = my_in6addr_any;
		sin6.sin6_port = htons(server_conf.tcp_port);
	}

	ksmbd_tcp_nodelay(ksmbd_socket);
	ksmbd_tcp_reuseaddr(ksmbd_socket);

#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 8, 0)
	ret = kernel_setsockopt(ksmbd_socket,
				SOL_SOCKET,
				SO_BINDTODEVICE,
				iface->name,
				strlen(iface->name));
#else
	ret = sock_setsockopt(ksmbd_socket,
			      SOL_SOCKET,
			      SO_BINDTODEVICE,
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 9, 0)
			      (char __user *)iface->name,
#else
			      KERNEL_SOCKPTR(iface->name),
#endif
			      strlen(iface->name));
#endif
	if (ret != -ENODEV && ret < 0) {
		ksmbd_err("Failed to set SO_BINDTODEVICE: %d\n", ret);
		goto out_error;
	}
	if (ipv4)
		ret = kernel_bind(ksmbd_socket, (struct sockaddr *)&sin,
				sizeof(sin));
	else
		ret = kernel_bind(ksmbd_socket, (struct sockaddr *)&sin6,
				sizeof(sin6));
	if (ret) {
		ksmbd_err("Failed to bind socket: %d (%s)\n", ret, iface->name);
		goto out_error;
	}

	ksmbd_socket->sk->sk_rcvtimeo = KSMBD_TCP_RECV_TIMEOUT;
	ksmbd_socket->sk->sk_sndtimeo = KSMBD_TCP_SEND_TIMEOUT;

	ret = kernel_listen(ksmbd_socket, KSMBD_SOCKET_BACKLOG);
	if (ret) {
		ksmbd_err("Port listen() error: %d\n", ret);
		goto out_error;
	}

	iface->ksmbd_socket = ksmbd_socket;
	ret = ksmbd_tcp_run_kthread(iface);
	if (ret) {
		ksmbd_err("Can't start ksmbd main kthread: %d\n", ret);
		goto out_error;
	}
	iface->state = IFACE_STATE_CONFIGURED;

	return 0;

out_error:
	tcp_destroy_socket(ksmbd_socket);
	iface->ksmbd_socket = NULL;
	return ret;
}

static int ksmbd_netdev_event(struct notifier_block *nb, unsigned long event,
		void *ptr)
{
#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 18, 0)
	struct net_device *netdev = ptr;
#else
	struct net_device *netdev = netdev_notifier_info_to_dev(ptr);
#endif
	struct interface *iface;
	int ret, found = 0;

	switch (event) {
	case NETDEV_UP:
		if (netdev->priv_flags & IFF_BRIDGE_PORT)
			return NOTIFY_OK;

		list_for_each_entry(iface, &iface_list, entry) {
			if (!strcmp(iface->name, netdev->name)) {
				found = 1;
				if (iface->state != IFACE_STATE_DOWN)
					break;
				ret = create_socket(iface);
				if (ret)
					return NOTIFY_OK;
				break;
			}
		}
		if (!found && bind_additional_ifaces) {
			iface = alloc_iface(kstrdup(netdev->name, GFP_KERNEL));
			if (!iface)
				return NOTIFY_OK;
			ret = create_socket(iface);
			if (ret)
				break;
		}
		break;
	case NETDEV_DOWN:
		list_for_each_entry(iface, &iface_list, entry) {
			if (!strcmp(iface->name, netdev->name) &&
			    iface->state == IFACE_STATE_CONFIGURED) {
				tcp_stop_kthread(iface->ksmbd_kthread);
				iface->ksmbd_kthread = NULL;
				mutex_lock(&iface->sock_release_lock);
				tcp_destroy_socket(iface->ksmbd_socket);
				iface->ksmbd_socket = NULL;
				mutex_unlock(&iface->sock_release_lock);

				iface->state = IFACE_STATE_DOWN;
				break;
			}
		}
		break;
 	}

	return NOTIFY_DONE;
}

static struct notifier_block ksmbd_netdev_notifier = {
	.notifier_call = ksmbd_netdev_event,
};

int ksmbd_tcp_init(void)
{
	register_netdevice_notifier(&ksmbd_netdev_notifier);

	return 0;
}

static void tcp_stop_kthread(struct task_struct *kthread)
{
	int ret;

	if (!kthread)
		return;

	ret = kthread_stop(kthread);
	if (ret)
		ksmbd_err("failed to stop forker thread\n");
}

void ksmbd_tcp_destroy(void)
{
	struct interface *iface, *tmp;

	unregister_netdevice_notifier(&ksmbd_netdev_notifier);
	list_for_each_entry_safe(iface, tmp, &iface_list, entry) {
		list_del(&iface->entry);
		kfree(iface->name);
		ksmbd_free(iface);
	}
}

static struct interface *alloc_iface(char *ifname)
{
	struct interface *iface;

	if (!ifname) {
		printk(KERN_ERR "Bad Interface in %s:%d\n", __func__,__LINE__);
		return NULL;
	}

	iface = ksmbd_zalloc(sizeof(struct interface));
	if (!iface) {
		printk(KERN_ERR "Out of memory in %s:%d\n", __func__,__LINE__);
		kfree(ifname);
		return NULL;
	}

	iface->name = ifname;
	iface->state = IFACE_STATE_DOWN;
	list_add(&iface->entry, &iface_list);
	mutex_init(&iface->sock_release_lock);
	return iface;
}

int ksmbd_tcp_set_interfaces(char *ifc_list, int ifc_list_sz)
{
	int sz = 0;

	if (!ifc_list_sz) {
		struct net_device *netdev;

		rtnl_lock();
		for_each_netdev(&init_net, netdev) {
			if (netdev->priv_flags & IFF_BRIDGE_PORT)
				continue;
			if (netdev->type != ARPHRD_ETHER || netdev->addr_len != ETH_ALEN ||
			    !is_valid_ether_addr(netdev->dev_addr))
				continue;
			if (!alloc_iface(kstrdup(netdev->name, GFP_KERNEL))) {
				printk(KERN_ERR "Out of memory in %s:%d\n", __func__,__LINE__);
				return -ENOMEM;
			}
		}
		rtnl_unlock();
		bind_additional_ifaces = 1;
		return 0;
	}

	while (ifc_list_sz > 0) {
		if (!alloc_iface(kstrdup(ifc_list, GFP_KERNEL))) {
			printk(KERN_ERR "Out of memory in %s:%d\n", __func__,__LINE__);
			return -ENOMEM;
		}

		sz = strlen(ifc_list);
		if (!sz)
			break;

		ifc_list += sz + 1;
		ifc_list_sz -= (sz + 1);
	}

	bind_additional_ifaces = 0;

	return 0;
}

static struct ksmbd_transport_ops ksmbd_tcp_transport_ops = {
	.read		= ksmbd_tcp_read,
	.writev		= ksmbd_tcp_writev,
	.disconnect	= ksmbd_tcp_disconnect,
};

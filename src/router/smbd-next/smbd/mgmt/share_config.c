// SPDX-License-Identifier: GPL-2.0-or-later
/*
 *   Copyright (C) 2018 Samsung Electronics Co., Ltd.
 */

#include <linux/list.h>
#include <linux/jhash.h>
#include <linux/slab.h>
#include <linux/rwsem.h>
#include <linux/parser.h>
#include <linux/namei.h>
#include <linux/sched.h>
#include <linux/mm.h>

#include "share_config.h"
#include "user_config.h"
#include "user_session.h"
#include "../buffer_pool.h"
#include "../transport_ipc.h"

#define SHARE_HASH_BITS		3
static DEFINE_HASHTABLE(shares_table, SHARE_HASH_BITS);
static DECLARE_RWSEM(shares_table_lock);

struct ksmbd_veto_pattern {
	char			*pattern;
	struct list_head	list;
};

static unsigned int share_name_hash(char *name)
{
	return jhash(name, strlen(name), 0);
}

static void kill_share(struct ksmbd_share_config *share)
{
	while (!list_empty(&share->veto_list)) {
		struct ksmbd_veto_pattern *p;

		p = list_entry(share->veto_list.next,
			       struct ksmbd_veto_pattern,
			       list);
		list_del(&p->list);
		kfree(p->pattern);
		kfree(p);
	}

	if (share->path)
		path_put(&share->vfs_path);
	kfree(share->name);
	kfree(share->path);
	ksmbd_free(share);
}

void __ksmbd_share_config_put(struct ksmbd_share_config *share)
{
	down_write(&shares_table_lock);
	hash_del(&share->hlist);
	up_write(&shares_table_lock);

	kill_share(share);
}

static struct ksmbd_share_config *
__get_share_config(struct ksmbd_share_config *share)
{
	if (!atomic_inc_not_zero(&share->refcount))
		return NULL;
	return share;
}

static struct ksmbd_share_config *__share_lookup(char *name)
{
	struct ksmbd_share_config *share;
	unsigned int key = share_name_hash(name);

	hash_for_each_possible(shares_table, share, hlist, key) {
		if (!strcmp(name, share->name))
			return share;
	}
	return NULL;
}

static int parse_veto_list(struct ksmbd_share_config *share,
			   char *veto_list,
			   int veto_list_sz)
{
	int sz = 0;

	if (!veto_list_sz)
		return 0;

	while (veto_list_sz > 0) {
		struct ksmbd_veto_pattern *p;

		sz = strlen(veto_list);
		if (!sz)
			break;

		p = ksmbd_zalloc(sizeof(struct ksmbd_veto_pattern));
		if (!p)
			return -ENOMEM;

		p->pattern = kstrdup(veto_list, GFP_KERNEL);
		if (!p->pattern) {
			printk(KERN_ERR "Out of memory in %s:%d\n", __func__,__LINE__);
			ksmbd_free(p);
			return -ENOMEM;
		}

		list_add(&p->list, &share->veto_list);

		veto_list += sz + 1;
		veto_list_sz -= (sz + 1);
	}

	return 0;
}

static struct ksmbd_share_config *share_config_request(char *name)
{
	struct ksmbd_share_config_response *resp;
	struct ksmbd_share_config *share = NULL;
	struct ksmbd_share_config *lookup;
	int ret;

	resp = ksmbd_ipc_share_config_request(name);
	if (!resp)
		return NULL;

	if (resp->flags == KSMBD_SHARE_FLAG_INVALID)
		goto out;

	share = ksmbd_zalloc(sizeof(struct ksmbd_share_config));
	if (!share)
		goto out;

	share->flags = resp->flags;
	atomic_set(&share->refcount, 1);
	INIT_LIST_HEAD(&share->veto_list);
	share->name = kstrdup(name, GFP_KERNEL);

	if (!test_share_config_flag(share, KSMBD_SHARE_FLAG_PIPE)) {
		share->path = kstrdup(KSMBD_SHARE_CONFIG_PATH(resp),
				      GFP_KERNEL);
		if (share->path)
			share->path_sz = strlen(share->path);
		share->create_mask = resp->create_mask;
		share->directory_mask = resp->directory_mask;
		share->force_create_mode = resp->force_create_mode;
		share->force_directory_mode = resp->force_directory_mode;
		share->force_uid = resp->force_uid;
		share->force_gid = resp->force_gid;
		ret = parse_veto_list(share,
				      KSMBD_SHARE_CONFIG_VETO_LIST(resp),
				      resp->veto_list_sz);
		if (!ret && share->path) {
			ret = kern_path(share->path, 0, &share->vfs_path);
			if (ret) {
				ksmbd_debug(SMB, "failed to access '%s'\n",
					share->path);
				/* Avoid put_path() */
				kfree(share->path);
				share->path = NULL;
			}
		}
		if (ret || !share->name) {
			kill_share(share);
			share = NULL;
			goto out;
		}
	}

	down_write(&shares_table_lock);
	lookup = __share_lookup(name);
	if (lookup)
		lookup = __get_share_config(lookup);
	if (!lookup) {
		hash_add(shares_table, &share->hlist, share_name_hash(name));
	} else {
		kill_share(share);
		share = lookup;
	}
	up_write(&shares_table_lock);

out:
	ksmbd_free(resp);
	return share;
}

static void strtolower(char *share_name)
{
	while (*share_name) {
		*share_name = tolower(*share_name);
		share_name++;
	}
}

struct ksmbd_share_config *ksmbd_share_config_get(char *name)
{
	struct ksmbd_share_config *share;

	strtolower(name);

	down_read(&shares_table_lock);
	share = __share_lookup(name);
	if (share)
		share = __get_share_config(share);
	up_read(&shares_table_lock);

	if (share)
		return share;
	return share_config_request(name);
}

#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 11, 0)
static bool match_wildcard(const char *pattern, const char *str)
{
	const char *s = str;
	const char *p = pattern;
	bool star = false;

	while (*s) {
		switch (*p) {
		case '?':
			s++;
			p++;
			break;
		case '*':
			star = true;
			str = s;
			if (!*++p)
				return true;
			pattern = p;
			break;
		default:
			if (*s == *p) {
				s++;
				p++;
			} else {
				if (!star)
					return false;
				str++;
				s = str;
				p = pattern;
			}
			break;
		}
	}

	if (*p == '*')
		++p;
	return !*p;
}
#endif

bool ksmbd_share_veto_filename(struct ksmbd_share_config *share,
			       const char *filename)
{
	struct ksmbd_veto_pattern *p;

	list_for_each_entry(p, &share->veto_list, list) {
		if (match_wildcard(p->pattern, filename))
			return true;
	}
	return false;
}

void ksmbd_share_configs_cleanup(void)
{
	struct ksmbd_share_config *share;
	struct hlist_node *tmp;
	int i;

	down_write(&shares_table_lock);
	hash_for_each_safe(shares_table, i, tmp, share, hlist) {
		hash_del(&share->hlist);
		kill_share(share);
	}
	up_write(&shares_table_lock);
}

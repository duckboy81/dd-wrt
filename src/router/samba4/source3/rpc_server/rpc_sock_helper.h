/*
 *  Unix SMB/CIFS implementation.
 *
 *  RPC Socket Helper
 *
 *  Copyright (c) 2011      Andreas Schneider <asn@samba.org>
 *
 *  This program is free software; you can redistribute it and/or modify
 *  it under the terms of the GNU General Public License as published by
 *  the Free Software Foundation; either version 3 of the License, or
 *  (at your option) any later version.
 *
 *  This program is distributed in the hope that it will be useful,
 *  but WITHOUT ANY WARRANTY; without even the implied warranty of
 *  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 *  GNU General Public License for more details.
 *
 *  You should have received a copy of the GNU General Public License
 *  along with this program; if not, see <http://www.gnu.org/licenses/>.
 */


#ifndef _RPC_SOCK_HELPER_H_
#define _RPC_SOCK_HELPER_H_

#include "rpc_server.h"

struct pf_listen_fd;

NTSTATUS dcesrv_create_ncacn_ip_tcp_sockets(
	struct dcesrv_endpoint *e,
	TALLOC_CTX *mem_ctx,
	size_t *pnum_fds,
	int **pfds);

#endif /* _RPC_SOCK_HELPER_H_ */

/* vim: set ts=8 sw=8 noet cindent syntax=c.doxygen: */

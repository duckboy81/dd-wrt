#
# Makefile for Broadcom BCM947XX boards
#
# Copyright (C) 2012, Broadcom Corporation. All Rights Reserved.
# 
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
# 
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION
# OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
# CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
#
# $Id: Makefile,v 1.8 2010-09-17 04:51:19 $
#

ifeq ($(CONFIG_CPU_MIPS32_R1),y)
EXTRA_CFLAGS+=-I$(srctree)/brcm/mipsel_r1/include -I$(srctree)/brcm/mipsel_r1/common/include   -I$(srctree)/brcm/mipsel_r1/shared/bcmwifi/include -DBCMDRIVER -Dlinux
SHARED := ../../../../brcm/mipsel_r1/shared
else
EXTRA_CFLAGS+=-I$(srctree)/brcm/mipsel/include -I$(srctree)/brcm/mipsel/common/include   -I$(srctree)/brcm/mipsel/shared/bcmwifi/include -DBCMDRIVER -Dlinux
SHARED := ../../../../brcm/mipsel/shared
endif

ifeq ($(CONFIG_MTD_NFLASH),y)
EXTRA_CFLAGS += -DNFLASH_SUPPORT -DCCNFLASH_SUPPORT
endif

BCM947XX_OBJS := $(addprefix $(SHARED)/, hndmips.o hndchipc.o )
BCM947XX_OBJS += $(addprefix $(SHARED)/, hndsflash.o ccsflash.o)
BCM947XX_OBJS += $(addprefix $(SHARED)/, nvram.o)
#BCM947XX_OBJS += sprom.o
EXTRA_CFLAGS += -DLINUX_HYBRID_POSTPROCESSING_REMOVAL

ifeq ($(CONFIG_PCI),y)
BCM947XX_OBJS += $(addprefix $(SHARED)/, hndpci.o) pcibios.o
endif

ifeq ($(CONFIG_MTD_NFLASH),y)
BCM947XX_OBJS += $(addprefix $(SHARED)/, hndnand.o)
BCM947XX_OBJS += $(addprefix $(SHARED)/, nflash_mips.o)
BCM947XX_OBJS += $(addprefix $(SHARED)/, nandcore.o)
BCM947XX_OBJS += $(addprefix $(SHARED)/, hndsoc.o)
endif

ifeq ($(CONFIG_MTD_BCMCONF_PARTS),y)
EXTRA_CFLAGS += -DBCMCONFMTD
endif

ifeq ($(CONFIG_GENERIC_GPIO),y)
BCM947XX_OBJS += gen_gpio.o
else
#BCM947XX_OBJS += $(addprefix $(SHARED)/, linux_gpio.o)
endif


BCM947XX_OBJS += setup.o prom.o time.o irq.o nvram_linux.o gpio.o cfe_env.o

# bcm947xx-objs += $(BCM947XX_OBJS)
# obj-y += bcm947xx.o

obj-y += $(BCM947XX_OBJS)

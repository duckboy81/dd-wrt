#obj-$(CONFIG_B53)		+= b53_common.o
obj-$(CONFIG_B53)		+= b5301x_common.o

obj-$(CONFIG_B53_PHY_FIXUP)	+= b53_phy_fixup.o

obj-$(CONFIG_B53_MMAP_DRIVER)	+= b53_mmap.o
#obj-$(CONFIG_B53_SRAB_DRIVER)	+= b53_srab.o
obj-$(CONFIG_B53_SRAB_DRIVER)	+= b5301x_srab.o
obj-$(CONFIG_B53_PHY_DRIVER)	+= b53_mdio.o
obj-$(CONFIG_B53_SPI_DRIVER)	+= b53_spi.o

ccflags-y			+= -Werror

#SPDX-License-Identifier: ISC

obj-$(CPTCFG_MT7915E) += mt7915e.o

mt7915e-y := pci.o init.o dma.o eeprom.o main.o mcu.o mac.o \
	     debugfs.o mmio.o

mt7915e-$(CPTCFG_NL80211_TESTMODE) += testmode.o

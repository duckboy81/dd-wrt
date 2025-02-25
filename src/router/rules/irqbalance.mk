irqbalance-configure:
	cd irqbalance && ./autogen.sh
	cd irqbalance && ./configure --disable-numa --prefix=/usr \
		--with-libcap_ng=no \
		--with-systemd=no \
		--without-irqbalance-ui \
		--enable-static=glib2 \
		--host=$(ARCH)-linux \
		CC="$(CC)" \
		GLIB2_CFLAGS="-I$(TOP)/glib20/libglib/glib -I$(TOP)/glib20/libglib -I$(TOP)/glib20/libglib/build/glib -L$(INSTALLDIR)/util-linux/usr/lib" \
		GLIB2_LIBS="-L$(TOP)/_staging_static/usr/lib -lglib-2.0" \
		CFLAGS="$(COPTS) $(MIPS16_OPT) $(LTO) -DNEED_PRINTF" \
		LDFLAGS="$(LDLTO)"

irqbalance: zlib libffi
	$(MAKE) -C irqbalance

irqbalance-clean: 
	if test -e "irqbalance/Makefile"; then $(MAKE) -C irqbalance clean ; fi

irqbalance-install: 
	$(MAKE) -C irqbalance install DESTDIR=$(INSTALLDIR)/irqbalance
	mkdir -p $(INSTALLDIR)/irqbalance/etc/irqbalance
	rm -rf $(INSTALLDIR)/irqbalance/etc
	rm -rf $(INSTALLDIR)/irqbalance/usr/include
	rm -rf $(INSTALLDIR)/irqbalance/usr/share
	
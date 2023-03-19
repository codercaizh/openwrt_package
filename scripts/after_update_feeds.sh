# dir: ./openwrt
## 更换n2n版本为最新版本
N2N_DIR=package/feeds/packages/n2n
rm -rf $N2N_DIR/patches
sed -i 's/PKG_VERSION:=3.0/PKG_VERSION:=3.1.1/g' $N2N_DIR/Makefile
sed -i 's/^PKG_HASH:.*/PKG_HASH:=skip/g' $N2N_DIR/Makefile
## 更换mt6721为linux5.15内核版本
# sed -i 's/KERNEL_PATCHVER:=5.4/KERNEL_PATCHVER:=5.15/g' target/linux/ramips/Makefile
# sed -i 's/+1,5/+1,6/g' package/kernel/mt76/patches/010-bypass-werror.patch
# sed -i 's/+EXTRA_CFLAGS += -DCONFIG_MT76_LEDS/+EXTRA_CFLAGS += -DCONFIG_MT76_LEDS\n+KBUILD_CFLAGS += -Wno-implicit-int/g' package/kernel/mt76/patches/010-bypass-werror.patch
## 用于对mt6721平台超频
sed -i 's/110,89/110,93/g' target/linux/ramips/patches-5.10/322-mt7621-fix-cpu-clk-add-clkdev.patch
sed -i 's/cpu_clk, bus_clk;/cpu_clk, bus_clk, i;/g' target/linux/ramips/patches-5.10/322-mt7621-fix-cpu-clk-add-clkdev.patch
sed -i 's/pll = rt_memc_r32(MEMC_REG_CPU_PLL);/pll = rt_memc_r32(MEMC_REG_CPU_PLL);\n+       pll \&= ~(0x7ff);\n+       pll |=  (0x362);\n+       rt_memc_w32(pll,MEMC_REG_CPU_PLL);\n+       for(i=0;i<1024;i++){}/g' target/linux/ramips/patches-5.10/322-mt7621-fix-cpu-clk-add-clkdev.patch
## 临时修复插件BUG
rm -rf package/feeds/small8/*mmdvm*

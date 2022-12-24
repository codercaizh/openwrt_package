# 执行脚本的目录在openwrt
## 更换n2n版本
N2N_DIR=package/feeds/packages/n2n
rm -rf $N2N_DIR/patches
sed -i 's/PKG_VERSION:=3.0/PKG_VERSION:=3.1.1/g' $N2N_DIR/Makefile
sed -i 's/^PKG_HASH:.*/PKG_HASH:=skip/g' $N2N_DIR/Makefile
sed -i 's/KERNEL_PATCHVER:=5.4/KERNEL_PATCHVER:=5.10/g' target/linux/ramips/Makefile
# 以下是超頻
sed -i 's/110,89/110,93/g' target/linux/ramips/patches-5.10/322-mt7621-fix-cpu-clk-add-clkdev.patch
sed -i 's/cpu_clk, bus_clk;/cpu_clk, bus_clk, i;/g' target/linux/ramips/patches-5.10/322-mt7621-fix-cpu-clk-add-clkdev.patch
sed -i 's/pll = rt_memc_r32(MEMC_REG_CPU_PLL);/pll = rt_memc_r32(MEMC_REG_CPU_PLL);\n+       pll \&= ~(0x7ff);\n+       pll |=  (0x362);\n+       rt_memc_w32(pll,MEMC_REG_CPU_PLL);\n+       for(i=0;i<1024;i++){}/g' target/linux/ramips/patches-5.10/322-mt7621-fix-cpu-clk-add-clkdev.patch

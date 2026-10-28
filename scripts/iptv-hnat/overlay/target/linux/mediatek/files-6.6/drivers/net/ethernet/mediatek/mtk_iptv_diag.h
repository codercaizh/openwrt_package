/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _MTK_IPTV_DIAG_H
#define _MTK_IPTV_DIAG_H
#include <linux/skbuff.h>
#include <linux/io.h>

#define MTK_IPTV_DIAG_ABI 1
#define IPTV_STAGES 40
#define IPTV_NF_BASE 8
#define IPTV_NF_EXIT_BASE 24
struct mtk_iptv_packet {
 __be32 src, dst;
 u32 ssrc, rxd, len, trigger_rxd, poll_id, desc_idx;
 int ifindex, mac, ring;
 u16 seq, vlan, sport, dport;
 bool valid;
};
extern u32 mtk_iptv_enable, mtk_iptv_nf_skip, mtk_iptv_wifi_skip;
extern u32 mtk_iptv_bypass, mtk_iptv_clear_hnat, mtk_iptv_no_gro;
extern int mtk_iptv_rx_mac;
extern u32 mtk_iptv_legacy_ppd;
u32 mtk_iptv_poll_id(void);
static inline bool mtk_iptv_active(void)
{
 return unlikely(READ_ONCE(mtk_iptv_enable) || READ_ONCE(mtk_iptv_bypass) ||
  READ_ONCE(mtk_iptv_clear_hnat) || READ_ONCE(mtk_iptv_no_gro) ||
  READ_ONCE(mtk_iptv_rx_mac) >= 0);
}
void mtk_iptv_init(void __iomem *base);
void mtk_iptv_exit(void);
void mtk_iptv_raw(struct mtk_iptv_packet *p, const void *data, u32 len,
                  u32 rxd, int mac, int ring);
void mtk_iptv_skb(struct mtk_iptv_packet *p, struct sk_buff *skb, u32 rxd);
void mtk_iptv_record(unsigned int stage, const struct mtk_iptv_packet *p, int action);
void mtk_iptv_count(unsigned int id);
#endif

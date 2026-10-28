// SPDX-License-Identifier: GPL-2.0
/* On-demand, bounded RTP diagnostics. No payloads, printk loop or packet allocation. */
#include <linux/debugfs.h>
#include <linux/etherdevice.h>
#include <linux/if_vlan.h>
#include <linux/ip.h>
#include <net/ip.h>
#include <linux/udp.h>
#include <linux/seq_file.h>
#include <linux/ktime.h>
#include <linux/slab.h>
#include <linux/module.h>
#include <asm/unaligned.h>
#include "mtk_iptv_diag.h"

u32 mtk_iptv_enable, mtk_iptv_nf_skip, mtk_iptv_wifi_skip;
u32 mtk_iptv_bypass, mtk_iptv_clear_hnat, mtk_iptv_no_gro;
int mtk_iptv_rx_mac = -1;
u32 mtk_iptv_legacy_ppd;
static atomic_t poll_serial = ATOMIC_INIT(0);
u32 mtk_iptv_poll_id(void) { return READ_ONCE(mtk_iptv_enable) ? atomic_inc_return(&poll_serial) : 0; }
EXPORT_SYMBOL_GPL(mtk_iptv_poll_id);
EXPORT_SYMBOL_GPL(mtk_iptv_legacy_ppd);
EXPORT_SYMBOL_GPL(mtk_iptv_enable);
EXPORT_SYMBOL_GPL(mtk_iptv_nf_skip);
EXPORT_SYMBOL_GPL(mtk_iptv_wifi_skip);
EXPORT_SYMBOL_GPL(mtk_iptv_bypass);
EXPORT_SYMBOL_GPL(mtk_iptv_clear_hnat);
EXPORT_SYMBOL_GPL(mtk_iptv_no_gro);
EXPORT_SYMBOL_GPL(mtk_iptv_rx_mac);

/* Host-order filter; 0 is wildcard. Defaults match the affected 4K channel. */
static u32 dst = 0xef0001a4, src, port = 3002, vlan, sample_every;
static void __iomem *fe_base;
static struct dentry *diag_root;
static DEFINE_SPINLOCK(diag_lock);
#define STREAMS 4
#define EVENTS 1024
struct stream {
 __be32 src;
 u32 ssrc;
 int ifindex;
 u16 highest;
 bool used;
 u64 seen, packets, gaps, gap_events, late, duplicates, outside_window;
};
struct stage_stats {
 u64 packets, bytes, actions[8], reasons[32], sports[16];
 struct stream streams[STREAMS];
 u64 overflow;
};
struct event {
 u64 ns;
 struct mtk_iptv_packet p;
 u32 stage, action;
 u16 previous;
 s16 delta;
};
static struct {
 struct stage_stats stages[IPTV_STAGES];
 struct event events[EVENTS];
 u64 count[64], events_written;
} diag;

static bool parse(struct mtk_iptv_packet *p, const u8 *buf, size_t n,
                  __be16 proto, unsigned int off)
{
 const struct iphdr *ip;
 unsigned int i, ihl;
 u16 udp_len;
 for (i = 0; i < 2 && eth_type_vlan(proto); i++) {
  if (n < off + 4) return false;
  p->vlan = get_unaligned_be16(buf + off) & VLAN_VID_MASK;
  proto = get_unaligned((__be16 *)(buf + off + 2)); off += 4;
 }
 if (proto != htons(ETH_P_IP) || n < off + sizeof(*ip)) return false;
 ip = (const struct iphdr *)(buf + off);
 if (ip->version != 4 || ip->ihl < 5 || ip->protocol != IPPROTO_UDP ||
     (ntohs(ip->frag_off) & (IP_MF | IP_OFFSET))) return false;
 p->src = get_unaligned(&ip->saddr); p->dst = get_unaligned(&ip->daddr);
 if ((READ_ONCE(dst) && ntohl(p->dst) != READ_ONCE(dst)) ||
     (READ_ONCE(src) && ntohl(p->src) != READ_ONCE(src))) return false;
 ihl = ip->ihl * 4; off += ihl;
 if (n < off + sizeof(struct udphdr) + 12) return false;
 p->sport = get_unaligned_be16(buf + off);
 p->dport = get_unaligned_be16(buf + off + 2);
 udp_len = get_unaligned_be16(buf + off + 4);
 if (udp_len < 20 || ntohs(ip->tot_len) < ihl + udp_len ||
     (READ_ONCE(port) && p->dport != READ_ONCE(port)) ||
     (READ_ONCE(vlan) && p->vlan != READ_ONCE(vlan))) return false;
 off += 8;
 if ((buf[off] >> 6) != 2) return false;
 p->seq = get_unaligned_be16(buf + off + 2);
 p->ssrc = get_unaligned_be32(buf + off + 8);
 p->valid = true;
 return true;
}

void mtk_iptv_raw(struct mtk_iptv_packet *p, const void *data, u32 len,
                  u32 rxd, int mac, int ring)
{
 memset(p, 0, sizeof(*p));
 if (!mtk_iptv_active() || len < ETH_HLEN) return;
 p->rxd = rxd; p->len = len; p->mac = mac; p->ring = ring;
 parse(p, data, min_t(u32, len, 128), get_unaligned((__be16 *)((u8 *)data + 12)), 14);
}
EXPORT_SYMBOL_GPL(mtk_iptv_raw);

void mtk_iptv_skb(struct mtk_iptv_packet *p, struct sk_buff *skb, u32 rxd)
{
 u8 head[128];
 unsigned int n;
 memset(p, 0, sizeof(*p));
 if (!mtk_iptv_active() || !skb) return;
 n = min_t(unsigned int, skb->len, sizeof(head));
 if (skb_copy_bits(skb, 0, head, n)) return;
 p->ifindex = skb->dev ? skb->dev->ifindex : 0;
 p->mac = -1; p->ring = -1; p->rxd = rxd; p->len = skb->len;
 if (skb_vlan_tag_present(skb)) p->vlan = skb_vlan_tag_get_id(skb);
 parse(p, head, n, skb->protocol, 0);
}
EXPORT_SYMBOL_GPL(mtk_iptv_skb);

void mtk_iptv_count(unsigned int id)
{
 unsigned long flags;
 if (!READ_ONCE(mtk_iptv_enable) || id >= ARRAY_SIZE(diag.count)) return;
 spin_lock_irqsave(&diag_lock, flags); diag.count[id]++; spin_unlock_irqrestore(&diag_lock, flags);
}
EXPORT_SYMBOL_GPL(mtk_iptv_count);

void mtk_iptv_record(unsigned int stage, const struct mtk_iptv_packet *p, int action)
{
 struct stage_stats *s;
 struct stream *t = NULL;
 struct event *e;
 unsigned long flags;
 unsigned int i, a = action & 7;
 u32 sampling = READ_ONCE(sample_every);
 u16 previous = 0;
 s16 delta = 0;
 bool log = false;
 if (!READ_ONCE(mtk_iptv_enable) || !p->valid || stage >= IPTV_STAGES) return;
 spin_lock_irqsave(&diag_lock, flags);
 s = &diag.stages[stage]; s->packets++; s->bytes += p->len; s->actions[a]++;
 /* MT7986 RX descriptor v1: entry[13:0], reason[18:14], sport[22:19]. */
 s->reasons[(p->rxd >> 14) & 31]++; s->sports[(p->rxd >> 19) & 15]++;
 for (i = 0; i < STREAMS; i++) {
  if (s->streams[i].used && s->streams[i].src == p->src &&
      s->streams[i].ssrc == p->ssrc && s->streams[i].ifindex == p->ifindex) {
   t = &s->streams[i]; break;
  }
 }
 if (!t) {
  for (i = 0; i < STREAMS; i++) if (!s->streams[i].used) {
   t = &s->streams[i]; t->used = true; t->src = p->src; t->ssrc = p->ssrc;
   t->ifindex = p->ifindex; t->highest = p->seq; t->seen = 1; log = true; break;
  }
  if (!t) s->overflow++;
 } else {
  previous = t->highest; delta = (s16)(p->seq - previous);
  if (delta > 0) {
   if (delta > 1) { t->gaps += delta - 1; t->gap_events++; log = true; }
   t->seen = delta >= 64 ? 1 : (t->seen << delta) | 1; t->highest = p->seq;
  } else if (delta > -64) {
   u64 bit = 1ULL << -delta;
   if (t->seen & bit) t->duplicates++;
   else { t->seen |= bit; t->late++; if (t->gaps) t->gaps--; }
  } else { t->outside_window++; log = true; }
 }
 if (t) t->packets++;
 /* Action 0=observe/accept, 1=drop, 2=stolen, 3=skip, 4=RX failure. */
 log |= action != 0;
 if (sampling && !(s->packets % sampling)) log = true;
 if (log) {
  e = &diag.events[diag.events_written++ % EVENTS];
  e->ns = ktime_get_ns(); e->p = *p; e->stage = stage; e->action = action;
  e->previous = previous; e->delta = delta;
 }
 spin_unlock_irqrestore(&diag_lock, flags);
}
EXPORT_SYMBOL_GPL(mtk_iptv_record);

static int stats_show(struct seq_file *m, void *unused)
{
 typeof(diag) *copy;
 unsigned long flags;
 unsigned int i, j;
 copy = kvmalloc(sizeof(*copy), GFP_KERNEL);
 if (!copy) return -ENOMEM;
 spin_lock_irqsave(&diag_lock, flags); memcpy(copy, &diag, sizeof(diag)); spin_unlock_irqrestore(&diag_lock, flags);
 seq_printf(m, "abi=%u enable=%u dst=%08x src=%08x port=%u vlan=%u nf_skip=%x wifi_skip=%x bypass=%u clear_hnat=%u no_gro=%u rx_mac=%d sample_every=%u legacy_sticky_ppd=%u\n",
  MTK_IPTV_DIAG_ABI, mtk_iptv_enable, dst, src, port, vlan, mtk_iptv_nf_skip, mtk_iptv_wifi_skip,
  mtk_iptv_bypass, mtk_iptv_clear_hnat, mtk_iptv_no_gro, mtk_iptv_rx_mac, sample_every, mtk_iptv_legacy_ppd);
 seq_printf(m, "events_written=%llu event_capacity=%u memory_bytes=%zu\n", copy->events_written, EVENTS, sizeof(diag));
 for (i = 0; i < ARRAY_SIZE(copy->count); i++) seq_printf(m, "counter[%u]=%llu\n", i, copy->count[i]);
 for (i = 0; i < IPTV_STAGES; i++) {
  struct stage_stats *s = &copy->stages[i];
  if (!s->packets) continue;
  seq_printf(m, "stage=%u packets=%llu bytes=%llu overflow=%llu actions=", i, s->packets, s->bytes, s->overflow);
  for (j = 0; j < 8; j++) seq_printf(m, "%u:%llu,", j, s->actions[j]);
  seq_puts(m, " reasons=");
  for (j = 0; j < 32; j++) if (s->reasons[j]) seq_printf(m, "%u:%llu,", j, s->reasons[j]);
  seq_puts(m, " sports=");
  for (j = 0; j < 16; j++) if (s->sports[j]) seq_printf(m, "%u:%llu,", j, s->sports[j]);
  seq_putc(m, '\n');
  for (j = 0; j < STREAMS; j++) {
   struct stream *t = &s->streams[j];
   if (!t->used) continue;
   seq_printf(m, "stream stage=%u src=%pI4 ssrc=%u ifindex=%d packets=%llu highest=%u missing_window64=%llu gap_events=%llu late=%llu duplicates=%llu outside_window=%llu\n",
    i, &t->src, t->ssrc, t->ifindex, t->packets, t->highest, t->gaps, t->gap_events, t->late, t->duplicates, t->outside_window);
  }
 }
 kvfree(copy); return 0;
}
DEFINE_SHOW_ATTRIBUTE(stats);

static int events_show(struct seq_file *m, void *unused)
{
 struct event *copy;
 unsigned long flags;
 u64 written, start, n;
 copy = kvmalloc_array(EVENTS, sizeof(*copy), GFP_KERNEL);
 if (!copy) return -ENOMEM;
 spin_lock_irqsave(&diag_lock, flags);
 written = diag.events_written; memcpy(copy, diag.events, sizeof(diag.events));
 spin_unlock_irqrestore(&diag_lock, flags);
 start = written > EVENTS ? written - EVENTS : 0;
 seq_printf(m, "written=%llu overwritten=%llu\n", written, start);
 for (n = start; n < written; n++) {
  struct event *e = &copy[n % EVENTS]; struct mtk_iptv_packet *p = &e->p;
  seq_printf(m, "ns=%llu stage=%u action=%u src=%pI4 dst=%pI4 ports=%u:%u ssrc=%u seq=%u previous=%u delta=%d vlan=%u ifindex=%d mac=%d ring=%d len=%u rxd=%08x reason=%u sport=%u entry=%u trigger_rxd=%08x poll=%u desc=%u\n",
   e->ns, e->stage, e->action, &p->src, &p->dst, p->sport, p->dport, p->ssrc,
   p->seq, e->previous, e->delta, p->vlan, p->ifindex, p->mac, p->ring, p->len,
   p->rxd, (p->rxd >> 14) & 31, (p->rxd >> 19) & 15, p->rxd & 0x3fff, p->trigger_rxd, p->poll_id, p->desc_idx);
 }
 kvfree(copy); return 0;
}
DEFINE_SHOW_ATTRIBUTE(events);

static int regs_show(struct seq_file *m, void *unused)
{
 static const u32 offsets[] = {0x500,0x1500,0x180,0x188,0x1a0,0x1a8,
  0x6100,0x6104,0x6108,0x610c,0x6204,0x6220,
  0x2200,0x2204,0x221c,0x2220,0x2228,0x2238,0x2320,
  0x2600,0x2604,0x261c,0x2620,0x2628,0x2638,0x2720};
 unsigned int i;
 if (!fe_base) return -ENODEV;
 seq_puts(m, "MT7986/NETSYS-v2 FE offsets; read-only, not an atomic hardware snapshot\n");
 for (i = 0; i < ARRAY_SIZE(offsets); i++) seq_printf(m, "%04x=%08x\n", offsets[i], readl(fe_base + offsets[i]));
 return 0;
}
DEFINE_SHOW_ATTRIBUTE(regs);

static int reset_set(void *data, u64 val)
{
 unsigned long flags;
 if (val != 1) return -EINVAL;
 spin_lock_irqsave(&diag_lock, flags); memset(&diag, 0, sizeof(diag)); spin_unlock_irqrestore(&diag_lock, flags);
 return 0;
}
DEFINE_DEBUGFS_ATTRIBUTE(reset_fops, NULL, reset_set, "%llu\n");
void mtk_iptv_init(void __iomem *base)
{
 if (diag_root) return;
 fe_base = base;
 diag_root = debugfs_create_dir("mtk_iptv_diag", NULL);
 if (IS_ERR(diag_root)) { diag_root = NULL; return; }
#define U32_FILE(name, var) debugfs_create_u32(name, 0600, diag_root, &var)
 U32_FILE("enable", mtk_iptv_enable); U32_FILE("dst", dst); U32_FILE("src", src);
 U32_FILE("port", port); U32_FILE("vlan", vlan); U32_FILE("sample_every", sample_every);
 U32_FILE("nf_skip_mask", mtk_iptv_nf_skip); U32_FILE("wifi_skip_mask", mtk_iptv_wifi_skip);
 U32_FILE("multicast_bypass", mtk_iptv_bypass); U32_FILE("rx_clear_hnat", mtk_iptv_clear_hnat);
 U32_FILE("no_gro", mtk_iptv_no_gro); U32_FILE("legacy_sticky_ppd", mtk_iptv_legacy_ppd);
#undef U32_FILE
 debugfs_create_u32("rx_mac", 0600, diag_root, (u32 *)&mtk_iptv_rx_mac);
 debugfs_create_file("stats", 0400, diag_root, NULL, &stats_fops);
 debugfs_create_file("events", 0400, diag_root, NULL, &events_fops);
 debugfs_create_file("registers", 0400, diag_root, NULL, &regs_fops);
 debugfs_create_file("reset", 0200, diag_root, NULL, &reset_fops);
}
void mtk_iptv_exit(void)
{
 debugfs_remove_recursive(diag_root); diag_root = NULL; fe_base = NULL;
}

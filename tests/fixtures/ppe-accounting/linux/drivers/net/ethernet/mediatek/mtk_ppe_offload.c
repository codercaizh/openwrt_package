// SPDX-License-Identifier: GPL-2.0-only
/* Excerpts: Linux 6.12.103 with ImmortalWrt v25.12.2 patches.
 * Unrelated declarations/functions omitted; not a full translation unit.
 */

static int
mtk_flow_offload_stats(struct mtk_eth *eth, struct flow_cls_offload *f)
{
	struct mtk_flow_entry *entry;
	u64 packets, bytes;
	int idle;

	entry = rhashtable_lookup(&eth->flow_table, &f->cookie,
				  mtk_flow_ht_params);
	if (!entry)
		return -ENOENT;

	packets = entry->packets;
	bytes = entry->bytes;
	mtk_foe_entry_get_stats(eth->ppe[entry->ppe_index], entry, &idle);
	f->stats.pkts += entry->packets - packets;
	f->stats.bytes += entry->bytes - bytes;
	f->stats.lastused = jiffies - idle * HZ;

	return 0;
}

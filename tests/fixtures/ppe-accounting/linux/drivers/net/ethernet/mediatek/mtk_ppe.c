// SPDX-License-Identifier: GPL-2.0-only
/* Excerpts: Linux 6.12.103 with ImmortalWrt v25.12.2 patches.
 * Unrelated declarations/functions omitted; not a full translation unit.
 */

static int __mtk_foe_entry_idle_time(struct mtk_ppe *ppe, u32 ib1)
{
	u32 ib1_ts_mask = mtk_get_ib1_ts_mask(ppe->eth);
	u16 now = mtk_eth_timestamp(ppe->eth);
	u16 timestamp = ib1 & ib1_ts_mask;

	if (timestamp > now)
		return ib1_ts_mask + 1 - timestamp + now;
	else
		return now - timestamp;
}

static bool
mtk_flow_entry_update(struct mtk_ppe *ppe, struct mtk_flow_entry *entry,
		      u64 *packets, u64 *bytes)
{
	struct mtk_foe_accounting *acct;
	struct mtk_foe_entry foe = {};
	struct mtk_foe_entry *hwe;
	u16 hash = entry->hash;
	bool ret = false;
	int len;

	if (hash == 0xffff)
		return false;

	hwe = mtk_foe_get_entry(ppe, hash);
	len = mtk_flow_entry_match_len(ppe->eth, &entry->data);
	memcpy(&foe, hwe, len);

	if (!mtk_flow_entry_match(ppe->eth, entry, &foe, len) ||
	    FIELD_GET(MTK_FOE_IB1_STATE, foe.ib1) != MTK_FOE_STATE_BIND) {
		acct = mtk_ppe_acct_data(ppe, hash);
		if (acct) {
			entry->prev_packets += acct->packets;
			entry->prev_bytes += acct->bytes;
		}

		goto out;
	}

	entry->data.ib1 = foe.ib1;
	acct = mtk_ppe_mib_entry_read(ppe, hash);
	ret = true;

out:
	if (acct) {
		*packets += acct->packets;
		*bytes += acct->bytes;
	}

	return ret;
}

static void
mtk_flow_entry_update_l2(struct mtk_ppe *ppe, struct mtk_flow_entry *entry)
{
	u32 ib1_ts_mask = mtk_get_ib1_ts_mask(ppe->eth);
	u64 *packets = &entry->packets;
	u64 *bytes = &entry->bytes;
	struct mtk_flow_entry *cur;
	struct hlist_node *tmp;
	int idle;

	idle = __mtk_foe_entry_idle_time(ppe, entry->data.ib1);
	hlist_for_each_entry_safe(cur, tmp, &entry->l2_flows, l2_list) {
		int cur_idle;

		if (!mtk_flow_entry_update(ppe, cur, packets, bytes)) {
			entry->prev_packets += cur->prev_packets;
			entry->prev_bytes += cur->prev_bytes;
			__mtk_foe_entry_clear(ppe, entry, false);
			continue;
		}

		cur_idle = __mtk_foe_entry_idle_time(ppe, cur->data.ib1);
		if (cur_idle >= idle)
			continue;

		idle = cur_idle;
		entry->data.ib1 &= ~ib1_ts_mask;
		entry->data.ib1 |= cur->data.ib1 & ib1_ts_mask;
	}
}

void mtk_foe_entry_get_stats(struct mtk_ppe *ppe, struct mtk_flow_entry *entry,
			     int *idle)
{
	entry->packets = entry->prev_packets;
	entry->bytes = entry->prev_bytes;

	spin_lock_bh(&ppe_lock);

	if (entry->type == MTK_FLOW_TYPE_L2)
		mtk_flow_entry_update_l2(ppe, entry);
	else
		mtk_flow_entry_update(ppe, entry, &entry->packets, &entry->bytes);

	*idle = __mtk_foe_entry_idle_time(ppe, entry->data.ib1);

	spin_unlock_bh(&ppe_lock);
}

void __mtk_ppe_check_skb(struct mtk_ppe *ppe, struct sk_buff *skb, u16 hash)
{
	const struct mtk_soc_data *soc = ppe->eth->soc;
	struct hlist_head *head = &ppe->foe_flow[hash / soc->hash_offset];
	struct mtk_foe_entry *hwe = mtk_foe_get_entry(ppe, hash);
	struct mtk_flow_entry *entry;
	struct mtk_foe_bridge key = {};
	struct mtk_foe_entry foe = {};
	struct hlist_node *n;
	struct ethhdr *eh;
	bool found = false;
	int entry_len;
	u8 *tag;

	spin_lock_bh(&ppe_lock);

	if (FIELD_GET(MTK_FOE_IB1_STATE, hwe->ib1) == MTK_FOE_STATE_BIND)
		goto out;

	entry_len = mtk_flow_entry_match_len(ppe->eth, hwe);
	memcpy(&foe, hwe, entry_len);

	hlist_for_each_entry_safe(entry, n, head, list) {
		if (found ||
		    !mtk_flow_entry_match(ppe->eth, entry, &foe, entry_len)) {
			if (entry->hash != 0xffff)
				__mtk_foe_entry_clear(ppe, entry, false);
			continue;
		}

		entry->hash = hash;
		__mtk_foe_entry_commit(ppe, &entry->data, hash);
		found = true;
	}

	if (found)
		goto out;

	eh = eth_hdr(skb);
	ether_addr_copy(key.dest_mac, eh->h_dest);
	ether_addr_copy(key.src_mac, eh->h_source);
	tag = skb->data - 2;
	key.vlan = 0;
	switch (skb->protocol) {
#if IS_ENABLED(CONFIG_NET_DSA)
	case htons(ETH_P_XDSA):
		if (!netdev_uses_dsa(skb->dev) ||
		    skb->dev->dsa_ptr->tag_ops->proto != DSA_TAG_PROTO_MTK)
			goto out;

		if (!skb_metadata_dst(skb))
			tag += 4;

		if (get_unaligned_be16(tag) != ETH_P_8021Q)
			break;

		fallthrough;
#endif
	case htons(ETH_P_8021Q):
		key.vlan = get_unaligned_be16(tag + 2) & VLAN_VID_MASK;
		break;
	default:
		break;
	}

	entry = rhashtable_lookup_fast(&ppe->l2_flows, &key, mtk_flow_l2_ht_params);
	if (!entry)
		goto out;

	mtk_foe_entry_commit_subflow(ppe, entry, hash);

out:
	spin_unlock_bh(&ppe_lock);
}

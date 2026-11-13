// SPDX-License-Identifier: GPL-2.0-only
/* Linux 6.18.60, with the ImmortalWrt 4765b657625e patch stack.
 * Excerpts for applying and testing the 6.18 accounting changes.
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
			__mtk_foe_entry_clear(ppe, cur, false);
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

static void
mtk_foe_entry_commit_subflow(struct mtk_ppe *ppe, struct mtk_flow_entry *entry,
			     u16 hash, int dscp, int rx_wed_index, u16 rx_wcid)
{
	const struct mtk_soc_data *soc = ppe->eth->soc;
	struct mtk_flow_entry *flow_info;
	struct mtk_foe_mac_info *l2;
	struct mtk_foe_entry *hwe;
	u32 ib1_mask = mtk_get_ib1_pkt_type_mask(ppe->eth) | MTK_FOE_IB1_UDP;
	int type;

	flow_info = kzalloc(sizeof(*flow_info), GFP_ATOMIC);
	if (!flow_info)
		return;

	flow_info->type = MTK_FLOW_TYPE_L2_SUBFLOW;
	flow_info->hash = hash;
	flow_info->wed_index = entry->wed_index;
	flow_info->wcid = entry->wcid;

	/* all subflows of a bridge entry come from one station */
	entry->rx_wed_index = rx_wed_index;
	entry->rx_wcid = rx_wcid;

	hwe = mtk_foe_get_entry(ppe, hash);
	memcpy(&flow_info->data, hwe, soc->foe_entry_size);
	flow_info->data.ib1 &= ib1_mask;
	flow_info->data.ib1 |= entry->data.ib1 & ~ib1_mask;

	l2 = mtk_foe_entry_l2(ppe->eth, &flow_info->data);
	memcpy(l2, &entry->data.bridge.l2, sizeof(*l2));

	type = mtk_get_ib1_pkt_type(ppe->eth, flow_info->data.ib1);
	if (type == MTK_PPE_PKT_TYPE_IPV4_HNAPT)
		memcpy(&flow_info->data.ipv4.new, &flow_info->data.ipv4.orig,
		       sizeof(flow_info->data.ipv4.new));
	else if (type >= MTK_PPE_PKT_TYPE_IPV6_ROUTE_3T && l2->etype == ETH_P_IP)
		l2->etype = ETH_P_IPV6;

	*mtk_foe_entry_ib2(ppe->eth, &flow_info->data) = entry->data.bridge.ib2;

	if (!mtk_foe_entry_set_prio(ppe, flow_info, &flow_info->data, dscp)) {
		kfree(flow_info);
		return;
	}

	hlist_add_head(&flow_info->list,
		       &ppe->foe_flow[hash / soc->hash_offset]);
	hlist_add_head(&flow_info->l2_list, &entry->l2_flows);

	__mtk_foe_entry_commit(ppe, &flow_info->data, hash);
}

void __mtk_ppe_check_skb(struct mtk_ppe *ppe, struct sk_buff *skb, u16 hash,
			 int rx_wed_index, u16 rx_wcid)
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
	int dscp;
	u8 *tag;

#if IS_ENABLED(CONFIG_NET_DSA)
	/* An untagged access frame does not expose its bridge PVID to PPE.
	 * Do not bind it using a MAC-only key shared by distinct VLANs.
	 */
	if (netdev_uses_dsa(skb->dev) &&
	    skb->dev->dsa_ptr->tag_ops->proto == DSA_TAG_PROTO_YT922X_4B) {
		struct dsa_port *dp;

		dsa_switch_for_each_user_port(dp, skb->dev->dsa_ptr->ds)
			if (dsa_port_is_vlan_filtering(dp))
				return;
	}
#endif

	spin_lock_bh(&ppe_lock);

	if (FIELD_GET(MTK_FOE_IB1_STATE, hwe->ib1) == MTK_FOE_STATE_BIND)
		goto out;

	entry_len = mtk_flow_entry_match_len(ppe->eth, hwe);
	memcpy(&foe, hwe, entry_len);

	dscp = mtk_ppe_skb_dscp(skb);

	hlist_for_each_entry_safe(entry, n, head, list) {
		if (found ||
		    !mtk_flow_entry_match(ppe->eth, entry, &foe, entry_len)) {
			if (entry->hash != 0xffff)
				__mtk_foe_entry_clear(ppe, entry, false);
			continue;
		}

		found = true;
		if (!mtk_foe_entry_set_prio(ppe, entry, &entry->data, dscp)) {
			entry->hash = 0xffff;
			continue;
		}

		entry->rx_wed_index = rx_wed_index;
		entry->rx_wcid = rx_wcid;
		entry->hash = hash;
		__mtk_foe_entry_commit(ppe, &entry->data, hash);
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
		if (!netdev_uses_dsa(skb->dev))
			goto out;

		switch (skb->dev->dsa_ptr->tag_ops->proto) {
		case DSA_TAG_PROTO_MTK:
			if (!skb_metadata_dst(skb))
				tag += 4;
			break;
		case DSA_TAG_PROTO_YT922X_4B:
			/* The outer 802.1Q-shaped header is a CPU port tag,
			 * not the bridge VID. Skip it before looking for an
			 * optional customer VLAN header, just as the tagger
			 * does later in the receive path.
			 */
			if (!pskb_may_pull(skb, 2 * VLAN_HLEN))
				goto out;
			tag = skb->data - 2;
			if (get_unaligned_be16(tag) != ETH_P_8021Q)
				goto out;
			tag += VLAN_HLEN;
			break;
		default:
			goto out;
		}

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

	mtk_foe_entry_commit_subflow(ppe, entry, hash, dscp, rx_wed_index,
				     rx_wcid);

out:
	spin_unlock_bh(&ppe_lock);
}

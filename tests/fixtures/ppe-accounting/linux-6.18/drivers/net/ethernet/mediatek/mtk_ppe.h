// SPDX-License-Identifier: GPL-2.0-only
/* Linux 6.18.60, with the ImmortalWrt 4765b657625e patch stack.
 * Excerpts for applying and testing the 6.18 accounting changes.
 */

struct mtk_flow_entry {
	union {
		/* regular flows + L2 subflows */
		struct {
			struct hlist_node list;
			struct hlist_node l2_list;
		};
		/* L2 flows */
		struct {
			struct rhash_head l2_node;
			struct hlist_head l2_flows;
		};
	};
	u8 type;
	s8 wed_index;
	s8 rx_wed_index;
	u8 ppe_index;
	u16 hash;
	u16 wcid;
	u16 rx_wcid;
	struct mtk_foe_entry data;
	struct rhash_head node;
	unsigned long cookie;
	u64 prev_packets, prev_bytes;
	u64 packets, bytes;
};

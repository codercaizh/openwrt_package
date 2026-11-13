// SPDX-License-Identifier: GPL-2.0-only
/* Linux 6.18.60, with the ImmortalWrt 4765b657625e patch stack.
 * Excerpts for applying and testing the 6.18 accounting changes.
 */

static int
mtk_flow_offload_stats(struct mtk_eth *eth, struct flow_cls_offload *f)
{
	struct mtk_flow_entry *entry;
	u64 packets, bytes;
	int idle;

	entry = rhashtable_lookup_fast(&eth->flow_table, &f->cookie,
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

static DEFINE_MUTEX(mtk_flow_offload_mutex);

int mtk_flow_offload_cmd(struct mtk_eth *eth, struct flow_cls_offload *cls,
			 int ppe_index)
{
	int err;

	mutex_lock(&mtk_flow_offload_mutex);
	switch (cls->command) {
	case FLOW_CLS_REPLACE:
		err = mtk_flow_offload_replace(eth, cls, ppe_index);
		break;
	case FLOW_CLS_DESTROY:
		err = mtk_flow_offload_destroy(eth, cls);
		break;
	case FLOW_CLS_STATS:
		err = mtk_flow_offload_stats(eth, cls);
		break;
	default:
		err = -EOPNOTSUPP;
		break;
	}
	mutex_unlock(&mtk_flow_offload_mutex);

	return err;
}

/* @rx is set for an entry that carries the frames of the station */
static bool
mtk_flow_entry_wcid_match(struct mtk_flow_entry *entry, int wed_index, u16 wcid,
			  bool *rx)
{
	*rx = false;
	if (entry->wed_index == wed_index && entry->wcid == wcid)
		return true;

	*rx = true;
	return entry->rx_wed_index == wed_index && entry->rx_wcid == wcid;
}

static void
mtk_flow_offload_walk_wcid(struct mtk_eth *eth, int wed_index, u16 wcid,
			   void (*fn)(struct mtk_ppe *ppe,
				      struct mtk_flow_entry *entry, bool rx,
				      void *arg),
			   void *arg)
{
	struct mtk_flow_entry *entry;
	struct rhashtable_iter iter;
	bool rx;

	mutex_lock(&mtk_flow_offload_mutex);

	rhashtable_walk_enter(&eth->flow_table, &iter);
	rhashtable_walk_start(&iter);
	while ((entry = rhashtable_walk_next(&iter)) != NULL) {
		if (IS_ERR(entry)) {
			if (PTR_ERR(entry) != -EAGAIN)
				break;
			continue;
		}

		if (!mtk_flow_entry_wcid_match(entry, wed_index, wcid, &rx))
			continue;

		fn(eth->ppe[entry->ppe_index], entry, rx, arg);
	}
	rhashtable_walk_stop(&iter);
	rhashtable_walk_exit(&iter);

	mutex_unlock(&mtk_flow_offload_mutex);
}

static void
mtk_flow_entry_unbind_cb(struct mtk_ppe *ppe, struct mtk_flow_entry *entry,
			 bool rx, void *arg)
{
	mtk_foe_entry_unbind(ppe, entry, rx, arg);
}

void mtk_flow_offload_unbind_wcid(struct mtk_eth *eth, int wed_index, u16 wcid,
				  struct mtk_flow_match *match)
{
	mtk_flow_offload_walk_wcid(eth, wed_index, wcid,
				   mtk_flow_entry_unbind_cb, match);
}

struct mtk_flow_idle {
	const struct mtk_flow_match *match;
	int idle;
};

static void
mtk_flow_entry_idle_cb(struct mtk_ppe *ppe, struct mtk_flow_entry *entry,
		       bool rx, void *arg)
{
	struct mtk_flow_idle *data = arg;
	int idle = mtk_foe_entry_flow_idle(ppe, entry, rx, data->match);

	if (idle >= 0 && (data->idle < 0 || idle < data->idle))
		data->idle = idle;
}

int mtk_flow_offload_idle_wcid(struct mtk_eth *eth, int wed_index, u16 wcid,
			       const struct mtk_flow_match *match)
{
	struct mtk_flow_idle data = {
		.match = match,
		.idle = -ENOENT,
	};

	mtk_flow_offload_walk_wcid(eth, wed_index, wcid,
				   mtk_flow_entry_idle_cb, &data);

	return data.idle;
}

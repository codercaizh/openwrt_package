// SPDX-License-Identifier: GPL-2.0-only
/* Excerpts: Linux 6.12.103 with ImmortalWrt v25.12.2 patches.
 * Unrelated declarations/functions omitted; not a full translation unit.
 */

static void flow_offload_work_add(struct flow_offload_work *offload)
{
	struct nf_flow_rule *flow_rule[FLOW_OFFLOAD_DIR_MAX];
	int err;

	err = nf_flow_offload_alloc(offload, flow_rule);
	if (err < 0)
		return;

	err = flow_offload_rule_add(offload, flow_rule);
	if (err < 0)
		goto out;

	set_bit(IPS_HW_OFFLOAD_BIT, &offload->flow->ct->status);

out:
	nf_flow_offload_destroy(flow_rule);
}

static void flow_offload_work_del(struct flow_offload_work *offload)
{
	clear_bit(IPS_HW_OFFLOAD_BIT, &offload->flow->ct->status);
	flow_offload_tuple_del(offload, FLOW_OFFLOAD_DIR_ORIGINAL);
	if (test_bit(NF_FLOW_HW_BIDIRECTIONAL, &offload->flow->flags))
		flow_offload_tuple_del(offload, FLOW_OFFLOAD_DIR_REPLY);
	set_bit(NF_FLOW_HW_DEAD, &offload->flow->flags);
}

static void flow_offload_tuple_stats(struct flow_offload_work *offload,
				     enum flow_offload_tuple_dir dir,
				     struct flow_stats *stats)
{
	nf_flow_offload_tuple(offload->flowtable, offload->flow, NULL, dir,
			      offload->flowtable->priority,
			      FLOW_CLS_STATS, stats,
			      &offload->flowtable->flow_block.cb_list);
}

static void flow_offload_work_stats(struct flow_offload_work *offload)
{
	struct flow_stats stats[FLOW_OFFLOAD_DIR_MAX] = {};
	u64 lastused;

	flow_offload_tuple_stats(offload, FLOW_OFFLOAD_DIR_ORIGINAL, &stats[0]);
	if (test_bit(NF_FLOW_HW_BIDIRECTIONAL, &offload->flow->flags))
		flow_offload_tuple_stats(offload, FLOW_OFFLOAD_DIR_REPLY,
					 &stats[1]);

	lastused = max_t(u64, stats[0].lastused, stats[1].lastused);
	offload->flow->timeout = max_t(u64, offload->flow->timeout,
				       lastused + flow_offload_get_timeout(offload->flow));

	if (offload->flowtable->flags & NF_FLOWTABLE_COUNTER) {
		if (stats[0].pkts)
			nf_ct_acct_add(offload->flow->ct,
				       FLOW_OFFLOAD_DIR_ORIGINAL,
				       stats[0].pkts, stats[0].bytes);
		if (stats[1].pkts)
			nf_ct_acct_add(offload->flow->ct,
				       FLOW_OFFLOAD_DIR_REPLY,
				       stats[1].pkts, stats[1].bytes);
	}
}

static void flow_offload_work_handler(struct work_struct *work)
{
	struct flow_offload_work *offload;
	struct net *net;

	offload = container_of(work, struct flow_offload_work, work);
	net = read_pnet(&offload->flowtable->net);
	switch (offload->cmd) {
		case FLOW_CLS_REPLACE:
			flow_offload_work_add(offload);
			NF_FLOW_TABLE_STAT_DEC_ATOMIC(net, count_wq_add);
			break;
		case FLOW_CLS_DESTROY:
			flow_offload_work_del(offload);
			NF_FLOW_TABLE_STAT_DEC_ATOMIC(net, count_wq_del);
			break;
		case FLOW_CLS_STATS:
			flow_offload_work_stats(offload);
			NF_FLOW_TABLE_STAT_DEC_ATOMIC(net, count_wq_stats);
			break;
		default:
			WARN_ON_ONCE(1);
	}

	clear_bit(NF_FLOW_HW_PENDING, &offload->flow->flags);
	kfree(offload);
}

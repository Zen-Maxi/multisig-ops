import json
import os
import pytest
from datetime import datetime
from decimal import Decimal

import numpy as np
import pandas as pd
from web3 import Web3, exceptions

from bal_tools import Subgraph, BalPoolsGauges


WATCHLIST = json.load(open("tools/python/gen_merkl_airdrops_watchlist.json"))
SUBGRAPH = Subgraph()
W3 = Web3(
    Web3.HTTPProvider(
        f"https://lb.drpc.org/ogrpc?network=ethereum&dkey={os.getenv('DRPC_KEY')}"
    )
)
EPOCH_DURATION = 60 * 60 * 24 * 14
FIRST_EPOCH_END = 1737936000

EPOCHS = []
ts = FIRST_EPOCH_END
while ts < int(datetime.now().timestamp()):
    EPOCHS.append(ts)
    ts += EPOCH_DURATION
epoch_name = f"epoch_{len(EPOCHS)}"
print("epochs:", EPOCHS)
print(epoch_name)


def get_user_shares_pool(pool, block):
    query = """query PoolShares($where: PoolShare_filter, $block: Block_height) {
        poolShares(where: $where, block: $block) {
            user {
                id
            }
            balance
        }
    }"""
    params = {
        "where": {
            "balance_gt": 0.001,
            "pool": pool.lower(),
        },
        "block": {"number": block},
    }
    raw = SUBGRAPH.fetch_graphql_data(
        "subgraphs-v3",
        query,
        params,
        url="https://api.studio.thegraph.com/query/75376/balancer-v3/version/latest",
        retries=5,
    )
    return dict([(x["user"]["id"], Decimal(x["balance"])) for x in raw["poolShares"]])


def get_user_shares_gauge(gauge, block):
    query = """query GaugeShares($where: GaugeShare_filter, $block: Block_height) {
        gaugeShares(where: $where, block: $block) {
            user {
                id
            }
            balance
        }
    }"""
    params = {
        "where": {
            "balance_gt": 0.001,
            "gauge": gauge.lower(),
        },
        "block": {"number": block},
    }
    raw = SUBGRAPH.fetch_graphql_data("gauges", query, params)
    return dict([(x["user"]["id"], Decimal(x["balance"])) for x in raw["gaugeShares"]])


def get_block_from_timestamp(ts):
    query = """query GetBlockFromTimestamp($where: Block_filter) {
        blocks(orderBy: "number", orderDirection: "desc", where: $where) {
            number
            timestamp
        }
    }"""
    params = {"where": {"timestamp_lte": ts}}
    raw = SUBGRAPH.fetch_graphql_data("blocks", query, params)
    return int(raw["blocks"][0]["number"])


def build_snapshot_df(
    pool,  # pool address
    end,  # timestamp of the last snapshot
    step_size=60 * 60 * 8,  # amount of seconds between snapshots
):
    gauge = BalPoolsGauges().get_preferential_gauge(pool)

    # get user shares for pool and gauge at different timestamps
    pool_shares = {}
    gauge_shares = {}
    start = end - EPOCH_DURATION
    while end > start:
        block = get_block_from_timestamp(end)
        pool_shares[block] = get_user_shares_pool(pool=pool, block=block)
        gauge_shares[block] = get_user_shares_gauge(gauge=gauge, block=block)
        end -= step_size

    # calculate total shares per user per block
    total_shares = {}
    total_supply = {}
    for block in pool_shares:
        total_shares[block] = {}
        for user_id in pool_shares[block]:
            if user_id == gauge.lower():
                # we do not want to count the gauge as a user
                continue
            total_shares[block][user_id] = pool_shares[block][user_id]
        for user_id in gauge_shares[block]:
            if user_id not in total_shares[block]:
                total_shares[block][user_id] = gauge_shares[block][user_id]
            else:
                total_shares[block][user_id] += gauge_shares[block][user_id]
        # collect onchain total supply per block
        contract = W3.eth.contract(
            address=Web3.to_checksum_address(pool),
            abi=json.load(open("tools/python/abis/StablePoolV3.json")),
        )
        try:
            total_supply[block] = contract.functions.totalSupply().call(
                block_identifier=block
            )
        except exceptions.BadFunctionCallOutput:
            total_supply[block] = 0

    # build dataframe
    df = pd.DataFrame(total_shares, dtype=float).fillna(0)

    # checksum total balances versus total supply
    assert df.sum().sum() == pytest.approx(sum(total_supply.values()) / 1e18, rel=1e-6)
    for block in df.columns:
        assert df[block].sum() == pytest.approx(total_supply[block] / 1e18, rel=1e-6)

    return df


def consolidate_shares(df):
    consolidated = pd.DataFrame()
    for block in df.columns:
        if df[block].sum() == 0:
            consolidated[block] = 0
        else:
            # calculate the percentage of the pool each user owns
            consolidated[block] = df[block] / df[block].sum()
            # weigh it by the total pool size of that block
            consolidated[block] *= df.sum()[block]
    # sum the weighted percentages per user
    consolidated["total"] = consolidated.sum(axis=1)
    # divide the weighted percentages by the sum of all weights
    consolidated["total"] = consolidated["total"] / df.sum().sum()
    return consolidated


def build_airdrop(reward_token, reward_total_wei, df):
    # https://docs.merkl.xyz/merkl-mechanisms/types-of-campaign/airdrop
    df[epoch_name] = df["total"].map(Decimal) * Decimal(reward_total_wei)
    df[epoch_name] = df[epoch_name].apply(np.floor).astype(str)
    df = df[df[epoch_name] != "0"]
    return {
        "rewardToken": reward_token,
        "rewards": df[[epoch_name]].to_dict(orient="index"),
    }


if __name__ == "__main__":
    for protocol in WATCHLIST:
        # TODO: aave not implemented yet
        if protocol == "aave":
            break
        for pool in WATCHLIST[protocol]["pools"]:
            print(protocol, pool)

            # get bpt balances for a pool at different timestamps
            df = build_snapshot_df(
                pool=WATCHLIST[protocol]["pools"][pool]["address"], end=EPOCHS[-1]
            )

            # consolidate user pool shares
            df = consolidate_shares(df)
            print(df)

            # morpho takes a 50bips fee on json airdrops
            if protocol == "morpho":
                reward_total_wei = int(
                    Decimal(WATCHLIST[protocol]["pools"][pool]["reward_wei"])
                    * Decimal(1 - 0.005)
                )
            else:
                reward_total_wei = int(WATCHLIST[protocol]["pools"][pool]["reward_wei"])

            # build airdrop object and dump to json file
            airdrop = build_airdrop(
                reward_token=WATCHLIST[protocol]["reward_token"],
                reward_total_wei=reward_total_wei,
                df=df,
            )

            # checksum
            total = Decimal(0)
            for user in airdrop["rewards"]:
                total += Decimal(airdrop["rewards"][user][epoch_name])
            assert total <= Decimal(WATCHLIST[protocol]["pools"][pool]["reward_wei"])
            print(
                "dust:",
                Decimal(reward_total_wei) - total,
            )

            json.dump(
                airdrop,
                open(
                    f"MaxiOps/merkl/airdrops/{protocol}-{pool}-{epoch_name}.json", "w"
                ),
                indent=2,
            )

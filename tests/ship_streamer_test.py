#!/usr/bin/env python3

import time
import json
import os
import shutil
import signal
import sys

from TestHarness import Cluster, TestHelper, Utils, WalletMgr, CORE_SYMBOL, createAccountKeys
from TestHarness.TestHelper import AppArgs

###############################################################
# ship_streamer_test
# 
# This test sets up 2 producing nodes and one "bridge" node using test_control_api_plugin.
#   One producing node has 3 of the elected producers and the other has 1 of the elected producers.
#   All the producers are named in alphabetical order, so that the 3 producers, in the one production node, are
#       scheduled first, followed by the 1 producer in the other producer node. Each producing node is only connected
#       to the other producing node via the "bridge" node.
#   The bridge node has the test_control_api_plugin, that the test uses to kill
#       the "bridge" node to generate a fork.
#   ship_streamer is used to connect to the state_history_plugin and verify that blocks receive link to previous
#   blocks. If the blocks do not link then ship_streamer will exit with an error causing this test to generate an
#   error. The fork generated by nodeos should be sent to the ship_streamer so it is able to correctly observe the
#   fork.
#
###############################################################

Print=Utils.Print

appArgs = AppArgs()
extraArgs = appArgs.add(flag="--num-clients", type=int, help="How many ship_streamers should be started", default=1)
args = TestHelper.parse_args({"--dump-error-details","--keep-logs","-v","--leave-running","--unshared"}, applicationSpecificArgs=appArgs)

Utils.Debug=args.v
cluster=Cluster(unshared=args.unshared, keepRunning=args.leave_running, keepLogs=args.keep_logs)
dumpErrorDetails=args.dump_error_details
walletPort=TestHelper.DEFAULT_WALLET_PORT

totalProducerNodes=2
totalNonProducerNodes=1
totalNodes=totalProducerNodes+totalNonProducerNodes
maxActiveProducers=21
totalProducers=maxActiveProducers

walletMgr=WalletMgr(True, port=walletPort)
testSuccessful=False

WalletdName=Utils.EosWalletName
shipTempDir=None

try:
    TestHelper.printSystemInfo("BEGIN")

    cluster.setWalletMgr(walletMgr)
    Print("Stand up cluster")

    # ***   setup topogrophy   ***

    # "bridge" shape connects defprocera through defproducerc (3 in node0) to each other and defproduceru (1 in node1)
    # and the only connection between those 2 groups is through the bridge node

    shipNodeNum = 1
    specificExtraNodeosArgs={}
    specificExtraNodeosArgs[shipNodeNum]="--plugin eosio::state_history_plugin --disable-replay-opts --trace-history --chain-state-history --plugin eosio::net_api_plugin "
    # producer nodes will be mapped to 0 through totalProducerNodes-1, so the number totalProducerNodes will be the non-producing node
    specificExtraNodeosArgs[totalProducerNodes]="--plugin eosio::test_control_api_plugin  "

    if cluster.launch(topo="bridge", pnodes=totalProducerNodes,
                      totalNodes=totalNodes, totalProducers=totalProducers,
                      specificExtraNodeosArgs=specificExtraNodeosArgs) is False:
        Utils.cmdError("launcher")
        Utils.errorExit("Failed to stand up eos cluster.")

    # ***   identify each node (producers and non-producing node)   ***

    #verify nodes are in sync and advancing
    cluster.waitOnClusterSync(blockAdvancing=5)
    Print("Cluster in Sync")

    prodNode = cluster.getNode(0)
    prodNode0 = prodNode
    prodNode1 = cluster.getNode(1)
    nonProdNode = cluster.getNode(2)
    shipNode = cluster.getNode(shipNodeNum)


    accounts=createAccountKeys(6)
    if accounts is None:
        Utils.errorExit("FAILURE - create keys")

    accounts[0].name="testeraaaaaa"
    accounts[1].name="tester111111" # needed for voting
    accounts[2].name="tester222222" # needed for voting
    accounts[3].name="tester333333" # needed for voting
    accounts[4].name="tester444444" # needed for voting
    accounts[5].name="tester555555" # needed for voting

    testWalletName="test"

    Print(f"Creating wallet {testWalletName}.")
    testWallet=walletMgr.create(testWalletName, [cluster.eosioAccount,accounts[0],accounts[1],accounts[2],accounts[3],accounts[4],accounts[5]])

    for _, account in cluster.defProducerAccounts.items():
        walletMgr.importKey(account, testWallet, ignoreDupKeyWarning=True)

    for i in range(0, totalNodes):
        node=cluster.getNode(i)
        node.producers=Cluster.parseProducers(i)
        for prod in node.producers:
            prodName = cluster.defProducerAccounts[prod].name
            if prodName == "defproducera" or prodName == "defproducerb" or prodName == "defproducerc" or prodName == "defproduceru":
                Print(f"Register producer {prodName}")
                trans=node.regproducer(cluster.defProducerAccounts[prod], "http://mysite.com", 0, waitForTransBlock=False, exitOnError=True)

    # create accounts via eosio as otherwise a bid is needed
    for account in accounts:
        Print(f"Create new account {account.name} via {cluster.eosioAccount.name} with private key: {account.activePrivateKey}")
        trans=nonProdNode.createInitializeAccount(account, cluster.eosioAccount, stakedDeposit=0, waitForTransBlock=True, stakeNet=10000, stakeCPU=10000, buyRAM=10000000, exitOnError=True)
        transferAmount="100000000.0000 {0}".format(CORE_SYMBOL)
        Print(f"Transfer funds {transferAmount} from account {cluster.eosioAccount.name} to {account.name}")
        nonProdNode.transferFunds(cluster.eosioAccount, account, transferAmount, "test transfer", waitForTransBlock=True)
        trans=nonProdNode.delegatebw(account, 20000000.0000, 20000000.0000, waitForTransBlock=False, exitOnError=True)

    # ***   vote using accounts   ***

    cluster.waitOnClusterSync(blockAdvancing=3)
    start_block_num = shipNode.getBlockNum()

    # vote a,b,c (node0)  u (node1)
    voteProducers=[]
    voteProducers.append("defproducera")
    voteProducers.append("defproducerb")
    voteProducers.append("defproducerc")
    voteProducers.append("defproduceru")
    for account in accounts:
        Print(f"Account {account.name} vote for producers={voteProducers}")
        trans=prodNode.vote(account, voteProducers, exitOnError=True, waitForTransBlock=False)

    #verify nodes are in sync and advancing
    cluster.waitOnClusterSync(blockAdvancing=3)
    Print("Shutdown unneeded bios node")
    cluster.biosNode.kill(signal.SIGTERM)
    prodNode0.waitForProducer("defproducerc")

    block_range = 350
    end_block_num = start_block_num + block_range

    shipClient = "tests/ship_streamer"
    cmd = f"{shipClient} --start-block-num {start_block_num} --end-block-num {end_block_num} --fetch-block --fetch-traces --fetch-deltas"
    if Utils.Debug: Utils.Print(f"cmd: {cmd}")
    clients = []
    files = []
    shipTempDir = os.path.join(Utils.DataDir, "ship")
    os.makedirs(shipTempDir, exist_ok = True)
    shipClientFilePrefix = os.path.join(shipTempDir, "client")

    starts = []
    for i in range(0, args.num_clients):
        start = time.perf_counter()
        outFile = open(f"{shipClientFilePrefix}{i}.out", "w")
        errFile = open(f"{shipClientFilePrefix}{i}.err", "w")
        Print(f"Start client {i}")
        popen=Utils.delayedCheckOutput(cmd, stdout=outFile, stderr=errFile)
        starts.append(time.perf_counter())
        clients.append((popen, cmd))
        files.append((outFile, errFile))
        Print(f"Client {i} started, Ship node head is: {shipNode.getBlockNum()}")

    # Generate a fork
    forkAtProducer="defproducera"
    prodNode1Prod="defproduceru"
    preKillBlockNum=nonProdNode.getBlockNum()
    preKillBlockProducer=nonProdNode.getBlockProducerByNum(preKillBlockNum)
    nonProdNode.killNodeOnProducer(producer=forkAtProducer, whereInSequence=1)
    Print(f"Current block producer {preKillBlockProducer} fork will be at producer {forkAtProducer}")
    prodNode0.waitForProducer(forkAtProducer)
    prodNode1.waitForProducer(prodNode1Prod)
    if nonProdNode.verifyAlive(): # if on defproducera, need to wait again
        prodNode0.waitForProducer(forkAtProducer)
        prodNode1.waitForProducer(prodNode1Prod)
    if nonProdNode.verifyAlive():
        Utils.errorExit("Bridge did not shutdown");
    Print("Fork started")

    prodNode0.waitForProducer("defproducerb") # wait for fork to progress a bit

    Print("Restore fork")
    Print("Relaunching the non-producing bridge node to connect the producing nodes again")
    if nonProdNode.verifyAlive():
        Utils.errorExit("Bridge is already running");
    if not nonProdNode.relaunch():
        Utils.errorExit(f"Failure - (non-production) node {nonProdNode.nodeNum} should have restarted")

    nonProdNode.waitForProducer(forkAtProducer)
    nonProdNode.waitForProducer(prodNode1Prod)
    afterForkBlockNum = nonProdNode.getBlockNum()
    if int(afterForkBlockNum) > int(end_block_num):
        Utils.errorExit(f"Did not stream long enough {end_block_num} to cover the fork {afterForkBlockNum}, increase block_range {block_range}")

    Print(f"Stopping all {args.num_clients} clients")
    for index, (popen, _), (out, err), start in zip(range(len(clients)), clients, files, starts):
        popen.wait()
        Print(f"Stopped client {index}.  Ran for {time.perf_counter() - start:.3f} seconds.")
        out.close()
        err.close()
        outFile = open(f"{shipClientFilePrefix}{index}.out", "r")
        data = json.load(outFile)
        block_num = start_block_num
        for i in data:
            # fork can cause block numbers to be repeated
            this_block_num = i['get_blocks_result_v0']['this_block']['block_num']
            if this_block_num < block_num:
                block_num = this_block_num
            assert block_num == this_block_num, f"{block_num} != {this_block_num}"
            assert isinstance(i['get_blocks_result_v0']['block'], str) # verify block in result
            block_num += 1
        assert block_num-1 == end_block_num, f"{block_num-1} != {end_block_num}"

    Print("Shutdown state_history_plugin nodeos")
    shipNode.kill(signal.SIGTERM)

    testSuccessful = True
finally:
    TestHelper.shutdown(cluster, walletMgr, testSuccessful=testSuccessful, dumpErrorDetails=dumpErrorDetails)
    if shipTempDir is not None:
        if testSuccessful and not args.keep_logs:
            shutil.rmtree(shipTempDir, ignore_errors=True)

errorCode = 0 if testSuccessful else 1
exit(errorCode)

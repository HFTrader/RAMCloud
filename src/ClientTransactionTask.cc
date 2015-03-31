/* Copyright (c) 2015 Stanford University
 *
 * Permission to use, copy, modify, and distribute this software for any
 * purpose with or without fee is hereby granted, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR(S) DISCLAIM ALL WARRANTIES
 * WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL AUTHORS BE LIABLE FOR
 * ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
 * WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
 * ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
 * OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

#include "ClientTransactionTask.h"
#include "RamCloud.h"

namespace RAMCloud {

/**
 * Constructor for a transaction task.
 *
 * \param ramcloud
 *      Overall information about the calling client.
 */
ClientTransactionTask::ClientTransactionTask(RamCloud* ramcloud)
    : ramcloud(ramcloud)
    , participantCount(0)
    , participantList()
    , state(INIT)
    , status(STATUS_OK)
    , decision(WireFormat::TxDecision::COMMIT)
    , lease()
    , txId(0)
    , prepareRpcs()
    , decisionRpcs()
    , commitCache()
    , nextCacheEntry()
{
}

/**
 * Find and return the cache entry identified by the given key.
 *
 * \param key
 *      Key of the object contained in the cache entry that should be returned.
 * \return
 *      Returns a pointer to the cache entry if found.  Returns NULL otherwise.
 *      Pointer is invalid once the commitCache is modified.
 */
ClientTransactionTask::CacheEntry*
ClientTransactionTask::findCacheEntry(Key& key)
{
    CacheKey cacheKey = {key.getTableId(), key.getHash()};
    CommitCacheMap::iterator it = commitCache.lower_bound(cacheKey);
    CacheEntry* entry = NULL;

    while (it != commitCache.end()) {
        if (cacheKey < it->first) {
            break;
        } else if (it->second.objectBuf) {
            Key otherKey(it->first.tableId,
                         it->second.objectBuf->getKey(),
                         it->second.objectBuf->getKeyLength());
            if (key == otherKey) {
                entry = &it->second;
                break;
            }
        }
        it++;
    }
    return entry;
}

/**
 * Return the transaction commit decision if a decision has been reached.
 * Otherwise, INVALID will be returned.
 */
WireFormat::TxDecision::Decision
ClientTransactionTask::getDecision()
{
    if (state != DECISION && state != DONE)
        return WireFormat::TxDecision::INVALID;
    return decision;
}

/**
 * Inserts a new cache entry with the provided key and value.  Other members
 * of the cache entry are left to their default values.  This method must not
 * be called once the transaction has started committing.
 *
 * \param tableId
 *      The table containing the desired object (return value from
 *      a previous call to getTableId).
 * \param key
 *      Variable length key that uniquely identifies the object within tableId.
 *      It does not necessarily have to be null terminated.
 * \param keyLength
 *      Size in bytes of the key.
 * \param buf
 *      Address of the first byte of the new contents for the object;
 *      must contain at least length bytes.
 * \param length
 *      Size in bytes of the new contents for the object.
 * \return
 *      Returns a pointer to the inserted cache entry.  Pointer is invalid
 *      once the commitCache is modified.
 */
ClientTransactionTask::CacheEntry*
ClientTransactionTask::insertCacheEntry(uint64_t tableId, const void* key,
        uint16_t keyLength, const void* buf, uint32_t length)
{
    Key keyObj(tableId, key, keyLength);
    CacheKey cacheKey = {keyObj.getTableId(), keyObj.getHash()};
    CommitCacheMap::iterator it = commitCache.insert(
            std::make_pair(cacheKey, CacheEntry()));
    it->second.objectBuf = new ObjectBuffer();
    Object::appendKeysAndValueToBuffer(
            keyObj, buf, length, it->second.objectBuf, true);
    return &it->second;
}

/**
 * Make incremental progress toward committing the transaction.
 */
void
ClientTransactionTask::performTask()
{
    try {
        if (state == INIT) {
            // Build participant list
            initTask();

            // TODO(cstlee) : handle buildParticipantList failure (namely
            // failure to aquire all the rpcIds we need).
            nextCacheEntry = commitCache.begin();
            state = PREPARE;
        }
        if (state == PREPARE) {
            processPrepareRpcs();
            sendPrepareRpc();
            if (prepareRpcs.empty() && nextCacheEntry == commitCache.end()) {
                nextCacheEntry = commitCache.begin();
                state = DECISION;
            }
        }
        if (state == DECISION) {
            processDecisionRpcs();
            sendDecisionRpc();
            if (decisionRpcs.empty() && nextCacheEntry == commitCache.end()) {
                ramcloud->rpcTracker.rpcFinished(txId);
                state = DONE;
            }
        }
    } catch (ClientException& e) {
        // If there are any problems with the commit protocol, STOP.
        prepareRpcs.clear();
        decisionRpcs.clear();
        status = e.status;
        state = DONE;
    }
}

/**
 * Initialize all necessary values of the commit task in preparation for the
 * commit protocol.  This includes building the send-ready buffer of
 * participants to be included in every prepare rpc and also the allocation of
 * rpcIds.  Used in the commit method.  Factored out mostly for ease of testing.
 */
void
ClientTransactionTask::initTask()
{
    lease = ramcloud->clientLease.getLease();
    txId = ramcloud->rpcTracker.newRpcId(1);    // TODO(cstlee) : see todo in
                                                // the method call.

    nextCacheEntry = commitCache.begin();
    while (nextCacheEntry != commitCache.end()) {
        const CacheKey* key = &nextCacheEntry->first;
        CacheEntry* entry = &nextCacheEntry->second;

        entry->rpcId = ramcloud->rpcTracker.newRpcId(1);    // TODO(cstlee) :
                                                            // see todo in the
                                                            // method call.
        participantList.emplaceAppend<WireFormat::TxParticipant>(
                key->tableId,
                static_cast<uint64_t>(key->keyHash),
                entry->rpcId);
        participantCount++;
        nextCacheEntry++;
    }
}

/**
 * Process any decision rpcs that have completed.  Used in the commit method.
 * Factored out mostly for ease of testing.
 */
void
ClientTransactionTask::processDecisionRpcs()
{
    // Process outstanding RPCs.
    std::list<DecisionRpc>::iterator it = decisionRpcs.begin();
    for (; it != decisionRpcs.end(); it++) {
        DecisionRpc* rpc = &(*it);

        if (!rpc->isReady()) {
            continue;
        }

        if (rpc->getState() == rpc->FAILED) {
            // Nothing to do.  Will be retried.
            TEST_LOG("FAILED");
        } else if (rpc->responseHeader->status == STATUS_OK) {
            TEST_LOG("STATUS_OK");
            for (uint32_t i = 0; i < rpc->reqHdr->participantCount; i++)
                ramcloud->rpcTracker.rpcFinished(rpc->ops[i]->second.rpcId);
        } else if (rpc->responseHeader->status == STATUS_UNKNOWN_TABLET) {
            // Nothing to do.  Will be retried.
            TEST_LOG("STATUS_UNKNOWN_TABLET");
        } else {
            ClientException::throwException(HERE, rpc->responseHeader->status);
        }

        // Destroy object.
        it = decisionRpcs.erase(it);
    }
}

/**
 * Process any prepare rpcs that have completed.  Used in the commit method.
 * Factored out mostly for ease of testing.
 */
void
ClientTransactionTask::processPrepareRpcs()
{
    // Process outstanding RPCs.
    std::list<PrepareRpc>::iterator it = prepareRpcs.begin();
    for (; it != prepareRpcs.end(); it++) {
        PrepareRpc* rpc = &(*it);

        if (!rpc->isReady()) {
            continue;
        }

        if (rpc->getState() == rpc->FAILED) {
            // Nothing to do.  Will be retried.
            TEST_LOG("FAILED");
        } else if (rpc->responseHeader->status == STATUS_OK) {
            WireFormat::TxPrepare::Response* respHdr =
                    rpc->response->getStart<WireFormat::TxPrepare::Response>();
            if (respHdr->vote != WireFormat::TxPrepare::COMMIT) {
                decision = WireFormat::TxDecision::ABORT;
            }
        } else if (rpc->responseHeader->status == STATUS_UNKNOWN_TABLET) {
            // Nothing to do.  Will be retried.
            TEST_LOG("STATUS_UNKNOWN_TABLET");
        } else {
            ClientException::throwException(HERE, rpc->responseHeader->status);
        }

        // Destroy object.
        it = prepareRpcs.erase(it);
    }
}

/**
 * Send out a decision rpc if not all master have been notified.  Used in the
 * commit method.  Factored out mostly for ease of testing.
 */
void
ClientTransactionTask::sendDecisionRpc()
{
    // Issue an additional rpc.
    DecisionRpc* nextRpc = NULL;
    Transport::SessionRef rpcSession;
    for (; nextCacheEntry != commitCache.end(); nextCacheEntry++) {
        const CacheKey* key = &nextCacheEntry->first;
        CacheEntry* entry = &nextCacheEntry->second;

        if (entry->state == CacheEntry::DECIDE) {
            continue;
        }

        if (nextRpc == NULL) {
            rpcSession =
                    ramcloud->objectFinder.lookup(key->tableId,
                                                  key->keyHash);
            decisionRpcs.emplace_back(ramcloud, rpcSession, this);
            nextRpc = &decisionRpcs.back();
        }

        Transport::SessionRef session =
                ramcloud->objectFinder.lookup(key->tableId, key->keyHash);
        if (session->getServiceLocator() == rpcSession->getServiceLocator()
            && nextRpc->reqHdr->participantCount <
                    DecisionRpc::MAX_OBJECTS_PER_RPC) {
            nextRpc->appendOp(nextCacheEntry);
        } else {
            break;
        }
    }
    if (nextRpc) {
        nextRpc->send();
    }
}

/**
 * Send out a prepare rpc if there are remaining un-prepared transaction ops.
 * Used in the commit method.  Factored out mostly for ease of testing.
 */
void
ClientTransactionTask::sendPrepareRpc()
{
    // Issue an additional rpc.
    PrepareRpc* nextRpc = NULL;
    Transport::SessionRef rpcSession;
    for (; nextCacheEntry != commitCache.end(); nextCacheEntry++) {
        const CacheKey* key = &nextCacheEntry->first;
        CacheEntry* entry = &nextCacheEntry->second;

        if (entry->state == CacheEntry::PREPARE) {
            continue;
        }

        if (nextRpc == NULL) {
            rpcSession =
                    ramcloud->objectFinder.lookup(key->tableId,
                                                  key->keyHash);
            prepareRpcs.emplace_back(ramcloud, rpcSession, this);
            nextRpc = &prepareRpcs.back();
        }

        Transport::SessionRef session =
                ramcloud->objectFinder.lookup(key->tableId, key->keyHash);
        if (session->getServiceLocator() == rpcSession->getServiceLocator()
            && nextRpc->reqHdr->opCount < PrepareRpc::MAX_OBJECTS_PER_RPC) {
            nextRpc->appendOp(nextCacheEntry);
        } else {
            break;
        }
    }
    if (nextRpc) {
        nextRpc->send();
    }
}

/**
 * Constructor for a decision rpc.
 *
 * \param ramcloud
 *      The RAMCloud object that governs this RPC.
 * \param session
 *      Session on which this RPC will eventually be sent.
 * \param task
 *      Pointer to the transaction task that issued this request.
 */
ClientTransactionTask::DecisionRpc::DecisionRpc(RamCloud* ramcloud,
        Transport::SessionRef session,
        ClientTransactionTask* task)
    : RpcWrapper(sizeof(WireFormat::TxDecision::Response))
    , ramcloud(ramcloud)
    , task(task)
    , ops()
    , reqHdr(allocHeader<WireFormat::TxDecision>())
{
    reqHdr->decision = task->decision;
    reqHdr->leaseId = task->lease.leaseId;
    reqHdr->participantCount = 0;
    this->session = session;
}

// See RpcWrapper for documentation.
bool
ClientTransactionTask::DecisionRpc::checkStatus()
{
    if (responseHeader->status == STATUS_UNKNOWN_TABLET) {
        retryRequest();
    }
    return true;
}

// See RpcWrapper for documentation.
bool
ClientTransactionTask::DecisionRpc::handleTransportError()
{
    // There was a transport-level failure. Flush cached state related
    // to this session, and related to the object mappings.  The objects
    // will all be retried when \c finish is called.
    if (session.get() != NULL) {
        ramcloud->clientContext->transportManager->flushSession(
                session->getServiceLocator());
        session = NULL;
    }
    retryRequest();
    return true;
}

// See RpcWrapper for documentation.
void
ClientTransactionTask::DecisionRpc::send()
{
    state = IN_PROGRESS;
    session->sendRequest(&request, response, this);
}

/**
 * Append an operation to the end of this decision rpc.
 *
 * \param opEntry
 *      Handle to information about the operation to be appended.
 */
void
ClientTransactionTask::DecisionRpc::appendOp(CommitCacheMap::iterator opEntry)
{
    const CacheKey* key = &opEntry->first;
    CacheEntry* entry = &opEntry->second;

    request.emplaceAppend<WireFormat::TxParticipant>(
            key->tableId,
            static_cast<uint64_t>(key->keyHash),
            entry->rpcId);

    entry->state = CacheEntry::DECIDE;
    ops[reqHdr->participantCount] = opEntry;
    reqHdr->participantCount++;
}

/**
 * Handle the case where the RPC may have been sent to the wrong server.
 */
void
ClientTransactionTask::DecisionRpc::retryRequest()
{
    for (uint32_t i = 0; i < reqHdr->participantCount; i++) {
        const CacheKey* key = &ops[i]->first;
        CacheEntry* entry = &ops[i]->second;
        ramcloud->objectFinder.flush(key->tableId);
        entry->state = CacheEntry::PENDING;
    }
    task->nextCacheEntry = task->commitCache.begin();
}

/**
 * Constructor for PrepareRpc.
 *
 * \param ramcloud
 *      The RAMCloud object that governs this RPC.
 * \param session
 *      Session on which this RPC will eventually be sent.
 * \param task
 *      Pointer to the transaction task that issued this request.
 */
ClientTransactionTask::PrepareRpc::PrepareRpc(RamCloud* ramcloud,
        Transport::SessionRef session, ClientTransactionTask* task)
    : RpcWrapper(sizeof(WireFormat::TxPrepare::Response))
    , ramcloud(ramcloud)
    , task(task)
    , ops()
    , reqHdr(allocHeader<WireFormat::TxPrepare>())
{
    reqHdr->lease = task->lease;
    reqHdr->participantCount = task->participantCount;
    reqHdr->opCount = 0;
    request.appendExternal(&task->participantList);
    this->session = session;
}

// See RpcWrapper for documentation.
bool
ClientTransactionTask::PrepareRpc::checkStatus()
{
    if (responseHeader->status == STATUS_UNKNOWN_TABLET) {
        retryRequest();
    }
    return true;
}

// See RpcWrapper for documentation.
bool
ClientTransactionTask::PrepareRpc::handleTransportError()
{
    // There was a transport-level failure. Flush cached state related
    // to this session, and related to the object mappings.  The objects
    // will all be retried when \c finish is called.
    if (session.get() != NULL) {
        ramcloud->clientContext->transportManager->flushSession(
                session->getServiceLocator());
        session = NULL;
    }
    retryRequest();
    return true;
}

// See RpcWrapper for documentation.
void
ClientTransactionTask::PrepareRpc::send()
{
    reqHdr->ackId = ramcloud->rpcTracker.ackId();
    state = IN_PROGRESS;
    session->sendRequest(&request, response, this);
}

/**
 * Append an operation to the end of this prepare rpc.
 *
 * \param opEntry
 *      Handle to information about the operation to be appended.
 */
void
ClientTransactionTask::PrepareRpc::appendOp(CommitCacheMap::iterator opEntry)
{
    const CacheKey* key = &opEntry->first;
    CacheEntry* entry = &opEntry->second;

    switch (entry->type) {
        case CacheEntry::READ:
            request.emplaceAppend<WireFormat::TxPrepare::Request::ReadOp>(
                    key->tableId, entry->rpcId,
                    entry->objectBuf->getKeyLength(), entry->rejectRules);
            request.appendExternal(entry->objectBuf->getKey(),
                    entry->objectBuf->getKeyLength());
            break;
        case CacheEntry::REMOVE:
            request.emplaceAppend<WireFormat::TxPrepare::Request::RemoveOp>(
                    key->tableId, entry->rpcId,
                    entry->objectBuf->getKeyLength(), entry->rejectRules);
            request.appendExternal(entry->objectBuf->getKey(),
                    entry->objectBuf->getKeyLength());
            break;
        case CacheEntry::WRITE:
            request.emplaceAppend<WireFormat::TxPrepare::Request::WriteOp>(
                    key->tableId, entry->rpcId,
                    entry->objectBuf->size(), entry->rejectRules);
            request.appendExternal(entry->objectBuf);
            break;
        default:
            RAMCLOUD_LOG(ERROR, "Unknown transaction op type.");
            return;
    }

    entry->state = CacheEntry::PREPARE;
    ops[reqHdr->opCount] = opEntry;
    reqHdr->opCount++;
}

/**
 * Handle the case where the RPC may have been sent to the wrong server.
 */
void
ClientTransactionTask::PrepareRpc::retryRequest()
{
    for (uint32_t i = 0; i < reqHdr->opCount; i++) {
        const CacheKey* key = &ops[i]->first;
        CacheEntry* entry = &ops[i]->second;
        ramcloud->objectFinder.flush(key->tableId);
        entry->state = CacheEntry::PENDING;
    }
    task->nextCacheEntry = task->commitCache.begin();
}

} // namespace RAMCloud

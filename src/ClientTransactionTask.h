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

#ifndef RAMCLOUD_CLIENTTRANSACTIONTASK_H
#define RAMCLOUD_CLIENTTRANSACTIONTASK_H

#include <list>
#include <map>
#include <memory>

#include "RamCloud.h"

namespace RAMCloud {

/**
 * The Client Transaction Task represents an ongoing or preparing transaction
 * commit.  This module is responsible for driving a client requested
 * transaction to completion.  It also allows the commit process to be handled
 * asynchronously by the RamCloud client library on the clients behalf.  For
 * efficiency, this module also stores the pre-commit values (the commit cache)
 * for the client transaction module.
 *
 * This module is driven by the ClientTransactionManager.
 */
class ClientTransactionTask {
  PUBLIC:
    /**
     * Structure to define the contents of the CommitCache.
     */
    struct CacheEntry {
        enum Type { READ, REMOVE, WRITE, INVALID };
        /// Type of the cached object entry.  Used to specify what kind of
        /// transaction operation needs to be performed during commit.
        Type type;
        /// Cached object value.  Used to service reads and store values for
        /// committing writes.  Ideally this would be a unique pointer to
        /// manage the memory automatically but std::multimap is missing the
        /// emplace feature.
        ObjectBuffer* objectBuf;
        /// Conditions upon which the transaction operation associated with
        /// this object should abort.
        RejectRules rejectRules;

        /// The rpcId to uniquely identify this operation.
        uint64_t rpcId;
        /// Used to keep track of what stage in the commit process this
        /// operation has reached.
        enum { PENDING, PREPARE, DECIDE, FAILED } state;

        /// Default constructor for CacheEntry.
        CacheEntry()
            : type(CacheEntry::INVALID)
            , objectBuf(NULL)
            , rejectRules({0, 0, 0, 0, 0})
            , rpcId(0)
            , state(PENDING)
        {}

        /// Copy constructor for CacheEntry, used to get around the missing
        /// emplace feature in std::multimap.
        explicit CacheEntry(const CacheEntry& other)
            : type(other.type)
            , objectBuf(other.objectBuf)
            , rejectRules(other.rejectRules)
            , rpcId(other.rpcId)
            , state(other.state)
        {}

        /// Destructor for CacheEntry.
        ///
        /// Warning: Multiple copies of CacheEntry objects may cause the
        /// ObjectBuffer pointed in the entry to be double freed.  This
        /// is indirectly due to missing emplace feature in std::multimap.
        ~CacheEntry()
        {
            if (objectBuf)
                delete objectBuf;
        }

        /// Assignment operator for CacheEntry, used to get around the missing
        /// emplace feature in std::multimap.
        CacheEntry& operator=(const CacheEntry& other)
        {
            if (this != &other) {
                type = other.type;
                objectBuf = other.objectBuf;
                rejectRules = other.rejectRules;
                rpcId = other.rpcId;
                state = other.state;
            }
            return *this;
        }
    };

    explicit ClientTransactionTask(RamCloud* ramcloud);

    CacheEntry* findCacheEntry(Key& key);
    WireFormat::TxDecision::Decision getDecision();
    Status getStatus() { return status; }
    CacheEntry* insertCacheEntry(uint64_t tableId, const void* key,
            uint16_t keyLength, const void* buf, uint32_t length);
    /// Check if the task as completed the commit protocol.
    bool isReady() { return (state == DONE); }
    void performTask();

  PRIVATE:
    // Forward declaration of RPCs
    class PrepareRpc;
    class DecisionRpc;

    /// Overall client state information.
    RamCloud* ramcloud;

    /// Number of participant objects/operations.
    uint32_t participantCount;
    /// List of participant object identifiers.
    Buffer participantList;

    /// Keeps track of the task currently executing phase.
    enum State { INIT, PREPARE, DECISION, DONE} state;

    /// Status of the transaction.  Used to defer exceptions.
    Status status;

    /// This transaction's decision to either COMMIT or ABORT.
    WireFormat::TxDecision::Decision decision;

    /// Lease information for to this transaction.
    WireFormat::ClientLease lease;

    /// Id of the rpcId that should be completed once the transaction is
    /// complete.
    uint64_t txId;

    /// List of "in flight" Prepare Rpcs.
    std::list<PrepareRpc> prepareRpcs;
    /// List of "in flight" Decision Rpcs.
    std::list<DecisionRpc> decisionRpcs;

    /**
     * Structure to define the key search value for the CommitCache map.
     */
    struct CacheKey {
        uint64_t tableId;       // tableId of the tablet
        KeyHash keyHash;        // start key hash value

        /**
         * The operator < is overridden to implement the
         * correct comparison for the CommitCache map.
         */
        bool operator<(const CacheKey& key) const {
            return tableId < key.tableId ||
                (tableId == key.tableId && keyHash < key.keyHash);
        }
    };

    /**
     * The Commit Cache is used to keep track of the  transaction operations to
     * be performed during commit and well as cache read and write values to
     * services subsequent reads.
     */
    typedef std::multimap<CacheKey, CacheEntry> CommitCacheMap;
    CommitCacheMap commitCache;

    /// Used to keep track of which cache entry to process next as part of the
    /// commit protocol.
    CommitCacheMap::iterator nextCacheEntry;

    void initTask();
    void processDecisionRpcs();
    void processPrepareRpcs();
    void sendDecisionRpc();
    void sendPrepareRpc();

    /// Encapsulates the state of a single Decision RPC sent to a single server.
    class DecisionRpc : public RpcWrapper {
        friend class ClientTransactionTask;
      public:
        DecisionRpc(RamCloud* ramcloud, Transport::SessionRef session,
                    ClientTransactionTask* task);
        ~DecisionRpc() {}

        bool checkStatus();
        bool handleTransportError();
        void send();

        void appendOp(CommitCacheMap::iterator opEntry);
        void retryRequest();

        /// Overall client state information.
        RamCloud* ramcloud;

        /// ClientTransactionTask that issued this rpc.
        ClientTransactionTask* task;

        /// Information about all of the ops that are being requested
        /// in this RPC.
#ifdef TESTING
        static const uint32_t MAX_OBJECTS_PER_RPC = 3;
#else
        static const uint32_t MAX_OBJECTS_PER_RPC = 75;
#endif
        CommitCacheMap::iterator ops[MAX_OBJECTS_PER_RPC];

        /// Header for the RPC (used to update count as objects are added).
        WireFormat::TxDecision::Request* reqHdr;

        DISALLOW_COPY_AND_ASSIGN(DecisionRpc);
    };

    /// Encapsulates the state of a single Prepare RPC sent to a single server.
    class PrepareRpc : public RpcWrapper {
        friend class ClientTransactionTask;
      public:
        PrepareRpc(RamCloud* ramcloud, Transport::SessionRef session,
                ClientTransactionTask* task);
        ~PrepareRpc() {}

        bool checkStatus();
        bool handleTransportError();
        void send();

        void appendOp(CommitCacheMap::iterator opEntry);
        void retryRequest();

        /// Overall client state information.
        RamCloud* ramcloud;

        /// ClientTransactionTask that issued this rpc.
        ClientTransactionTask* task;

        /// Information about all of the ops that are being requested
        /// in this RPC.
#ifdef TESTING
        static const uint32_t MAX_OBJECTS_PER_RPC = 3;
#else
        static const uint32_t MAX_OBJECTS_PER_RPC = 75;
#endif
        CommitCacheMap::iterator ops[MAX_OBJECTS_PER_RPC];

        /// Header for the RPC (used to update count as objects are added).
        WireFormat::TxPrepare::Request* reqHdr;

        DISALLOW_COPY_AND_ASSIGN(PrepareRpc);
    };

    DISALLOW_COPY_AND_ASSIGN(ClientTransactionTask);
};

} // end RAMCloud

#endif  /* RAMCLOUD_CLIENTTRANSACTIONTASK_H */

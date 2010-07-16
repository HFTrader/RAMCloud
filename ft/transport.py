# Copyright (c) 2010 Stanford University
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR(S) DISCLAIM ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL AUTHORS BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

import random
import struct

from util import gettime, Buffer, BitVector, Ring

TEST_ADDRESS = ('127.0.0.1', 12242)

# TODO(ongaro): Should timeouts be on the RPC object?

#### Client only

NS_PER_MS = 1000 * 1000
TIMEOUT_NS = 10 * NS_PER_MS

TIMEOUTS_UNTIL_ABORTING = 500 # >= 5s

# The server will advertise that n channels are available on session open.
# The client can then use any subset of those.
MAX_NUM_CHANNELS_PER_SESSION = 8

#### Server only

# The maximum number of channels to allocate per session.
NUM_CHANNELS_PER_SESSION = 8

# The time until the server will close an inactive session.
SESSION_TIMEOUT_NS = 60 * 60 * 1000 * 1000 * 1000 # 30min

#### Client and Server (must be same)

# The width in bits of the RPC ID field.
RPCID_WIDTH = 32

# The number of fragments a receiving end is willing to accept beyond the
# smallest fragment number that it has not acknowledged.
MAX_STAGING_FRAGMENTS = 32

#### Client and Server (may differ)

# The fraction of packets that will be dropped on transmission.
# This should be 0 for production!
PACKET_LOSS = 0.05

WINDOW_SIZE = 10
REQ_ACK_AFTER = 5
assert 0 <= REQ_ACK_AFTER <= WINDOW_SIZE <= MAX_STAGING_FRAGMENTS + 1

DEBUGGING = True

"""
Naming conventions:

_transport refers to the Transport object.

_session refers to a Session, either a ClientSession or a ServerSession. Often
only one of those makes sense for the context.

_channel refers to a Channel, either a ClientChannel or a ServerChannel. Often
only one of those makes sense for the context.

_state is present in many of the classes that act as state machines and will
refer to one of the class's _*_STATE members.

_lastActivityTime is the time immediately after the latest packet (of any type)
was sent to or received from the other end.
"""

def debug(s):
    if DEBUGGING:
        print s

newObjects = []

def new(x):
    newObjects.append(x)
    return x

def delete(x):
    newObjects.remove(x)

class PayloadChunk(object):
    @staticmethod
    def appendToBuffer(dataBuffer, driver, data, payload, length):
        dataBuffer.append(data)

        # In C++, release would be called in the Chunk's destructor, but
        # I don't feel like implementing Buffer in its full glory here.
        driver.release(payload, length)

class PayloadReleaser(object):
    def __init__(self, driver, payload, length):
        self._driver = driver
        self.payload = payload
        self.length = length

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.payload is not None:
            self._driver.release(self.payload, self.length)

    def steal(self):
        try:
            return self.payload, self.length
        finally:
            self.payload = None
            self.length = None

class Header(object):
    """
    A binary header that goes at the start of every message (same for request
    and response).

    This would be implemented as a simple struct in C++.

    Wire format::
      <---------------32 bits -------------->
      +-------------------------------------+
      |            sessionToken             |
      +-------------------------------------+
      |        sessionToken (cont.)         |
      +-------------------------------------+
      |                rpcId                |
      +-------------------------------------+
      |         clientSessionHint           |
      +-------------------------------------+
      |         serverSessionHint           |
      +-------------------------------------+
      |     fragNumber    |  totalFrags     |
      +-------------------------------------+
      | channelId | flags |
      +-------------------+
    Everything is encoded in little-endian, NOT network byte order.

    @cvar LENGTH: The size in bytes of a Header.
    """

    _PACK_FORMAT = 'QIIIHHBB'
    assert RPCID_WIDTH <= 32

    LENGTH = struct.calcsize(_PACK_FORMAT)

    # direction
    CLIENT_TO_SERVER = 0
    SERVER_TO_CLIENT = 1

    # flags
    PAYLOAD_TYPE_MASK = 0xF0
    DIRECTION_MASK    = 0x01
    REQUEST_ACK_MASK  = 0x02
    PLEASE_DROP_MASK  = 0x04

    # payload types
    PT_DATA         = 0x00
    PT_ACK          = 0x10
    PT_SESSION_OPEN = 0x20
    PT_RESERVED_1   = 0x30
    PT_BAD_SESSION  = 0x40
    PT_RETRY_WITH_NEW_RPCID = 0x50
    PT_RESERVED_2   = 0x60
    PT_RESERVED_3   = 0x70

    @classmethod
    def fromString(cls, string):
        """Unpack a Header from a string."""
        unpacked = struct.unpack(cls._PACK_FORMAT, string)
        flags = unpacked[-1]
        return cls(sessionToken=unpacked[0],
                   rpcId=unpacked[1],
                   clientSessionHint=unpacked[2],
                   serverSessionHint=unpacked[3],
                   fragNumber=unpacked[4],
                   totalFrags=unpacked[5],
                   channelId=unpacked[6],
                   payloadType=(flags & cls.PAYLOAD_TYPE_MASK),
                   direction=(flags & cls.DIRECTION_MASK),
                   requestAck=bool((flags & cls.REQUEST_ACK_MASK) != 0),
                   pleaseDrop=bool((flags & cls.PLEASE_DROP_MASK) != 0))

    def __init__(self,
                 sessionToken=None,
                 rpcId=None,
                 clientSessionHint=None,
                 serverSessionHint=None,
                 fragNumber=0,
                 totalFrags=1,
                 channelId=None,
                 payloadType=None,
                 direction=None,
                 requestAck=False,
                 pleaseDrop=False):
        self.sessionToken = sessionToken
        self.rpcId = rpcId
        self.clientSessionHint = clientSessionHint
        self.serverSessionHint = serverSessionHint
        self.fragNumber = fragNumber
        self.totalFrags = totalFrags
        self.channelId = channelId
        self.payloadType = payloadType
        self.direction = direction
        self.requestAck = requestAck
        self.pleaseDrop = pleaseDrop

    def __str__(self):
        assert self.sessionToken is not None
        assert self.rpcId is not None
        assert self.fragNumber is not None
        assert self.totalFrags is not None
        assert self.clientSessionHint is not None
        assert self.serverSessionHint is not None
        assert self.channelId is not None
        assert self.fragNumber < self.totalFrags
        if self.requestAck:
            assert self.payloadType == self.PT_DATA
        if self.direction == self.CLIENT_TO_SERVER:
            assert self.payloadType in [self.PT_DATA, self.PT_ACK,
                                        self.PT_SESSION_OPEN]

        flags = 0
        if self.direction:   flags |= self.DIRECTION_MASK
        if self.requestAck:  flags |= self.REQUEST_ACK_MASK
        if self.pleaseDrop:  flags |= self.PLEASE_DROP_MASK
        flags |= self.payloadType
        return struct.pack(self._PACK_FORMAT,
                           self.sessionToken,
                           self.rpcId,
                           self.clientSessionHint,
                           self.serverSessionHint,
                           self.fragNumber,
                           self.totalFrags,
                           self.channelId,
                           flags)

class SessionOpenResponse(object):
    PACK_FORMAT = 'B'
    LENGTH = struct.calcsize(PACK_FORMAT)

    @classmethod
    def fromString(cls, string):
        (maxChannelId,) = struct.unpack(cls.PACK_FORMAT, string)
        return cls(maxChannelId)

    def __init__(self, maxChannelId):
        self.maxChannelId = maxChannelId

    def __str__(self):
        b = Buffer()
        self.fillBuffer(b)
        return b.getRange(0, b.getTotalLength())

    def fillBuffer(self, bufferToFill):
        bufferToFill.prepend(struct.pack(self.PACK_FORMAT, self.maxChannelId))

class AckResponse(object):
    """
    The format of the payload of messages of type Header.PT_ACK.
    """
    PACK_FORMAT = 'H'

    HEADER_LENGTH = struct.calcsize(PACK_FORMAT)
    STAGING_VECTOR_LENGTH = (MAX_STAGING_FRAGMENTS + 7) / 8
    LENGTH = HEADER_LENGTH + STAGING_VECTOR_LENGTH

    @classmethod
    def fromString(cls, string):
        """Unpack an AckResponse from a string."""
        (firstMissingFrag,) = struct.unpack(cls.PACK_FORMAT,
                                               string[:cls.HEADER_LENGTH])
        stagingVector = BitVector(MAX_STAGING_FRAGMENTS,
                                  seq=string[cls.HEADER_LENGTH:])
        return cls(firstMissingFrag, stagingVector)

    def __init__(self, firstMissingFrag, stagingVector=None):
        self.firstMissingFrag = firstMissingFrag
        if stagingVector is None:
            self.stagingVector = BitVector(MAX_STAGING_FRAGMENTS)
        else:
            self.stagingVector = stagingVector

    def __str__(self):
        b = Buffer()
        self.fillBuffer(b)
        return b.getRange(0, b.getTotalLength())

    def fillBuffer(self, bufferToFill):
        self.stagingVector.fillBuffer(bufferToFill)
        bufferToFill.prepend(struct.pack(self.PACK_FORMAT,
                                         self.firstMissingFrag))

class InboundMessage(object):
    """
    A partially-received data message (either a request or a response).

    This handles assembling the message fragments and responding to ACK
    requests. It is used in server channels for the client's request and in
    client channels for the server's response.

    @ivar _transport: x
    @ivar _session: x
    @ivar _channelId: x
    @ivar _lastActivityTime: x
    @ivar _totalFrags:
        The number of fragments that make up the message to be received.
    @ivar _firstMissingFrag:
        The number before which all fragments have been received, in the range
        [0, _totalFrags]. The data for every fragment before _firstMissingFrag
        will have been added to _dataBuffer, while data for fragments following
        _firstMissingFrag may be found in _dataStagingRing.
    @ivar _dataBuffer:
        A Buffer that is filled with the contents of the message. This always
        contains data for the fragments in the range [0, _firstMissingFrag).
    @ivar _dataStagingRing:
        A staging area for packet data until it can be appended to _dataBuffer.
        A Ring of MAX_STAGING_FRAGMENTS pointers to packet data or None, where
        each entry corresponds with the _firstMissingFrag + 1 + i-th packet.
        (Note that the _firstMissingFrag fragment has no packet data by
        definition.)
    """

    def _sendAck(self):
        """Send the server an ACK for the received response packets.

        Caller should update _lastActivityTime.
        """
        header = Header()
        self._session.fillHeader(header, self._channelId)
        header.payloadType = Header.PT_ACK
        ackResponse = AckResponse(self._firstMissingFrag)
        for i, (payload, length) in enumerate(self._dataStagingRing):
            if payload is not None:
                ackResponse.stagingVector.setBit(i)
        payloadBuffer = Buffer()
        ackResponse.fillBuffer(payloadBuffer)
        self._transport._sendOne(self._session.getAddress(), header,
                                 payloadBuffer)

    def __init__(self, transport, session, channelId):
        self._transport = transport
        self._session = session
        self._channelId = channelId
        self._totalFrags = None
        self._firstMissingFrag = None
        self._dataStagingRing = Ring(MAX_STAGING_FRAGMENTS, (None, None))
        self._dataBuffer = None
        self._lastActivityTime = 0

    def init(self, totalFrags, dataBuffer):
        self._totalFrags = totalFrags
        self._firstMissingFrag = 0
        self._dataBuffer = dataBuffer
        self._lastActivityTime = 0
        for payload, length in self._dataStagingRing:
            if payload is not None:
                self._transport._driver.release(payload, length)
        self._dataStagingRing.clear()

    def __del__(self):
        self.init(None, None)

    def getLastActivityTime(self):
        return self._lastActivityTime

    def processReceivedData(self, payloadCM):
        """
        @return:
            Whether the full message has been received and added to the
            dataBuffer.
        """
        header = Header.fromString(payloadCM.payload[:Header.LENGTH])

        if header.totalFrags != self._totalFrags:
            # The other end is retarded?
            return (self._firstMissingFrag == self._totalFrags)
        if header.fragNumber == self._firstMissingFrag:

            payload, length = payloadCM.steal()
            PayloadChunk.appendToBuffer(self._dataBuffer,
                                        self._transport._driver,
                                        payload[Header.LENGTH:],
                                        payload, length)

            self._firstMissingFrag += 1
            while True: # num iterations bounded to MAX_STAGING_FRAGMENTS-ish
                payload, length = self._dataStagingRing[0]
                self._dataStagingRing.advance(1)
                if payload is None:
                    break
                PayloadChunk.appendToBuffer(self._dataBuffer,
                                            self._transport._driver,
                                            payload[Header.LENGTH:],
                                            payload, length)
                self._firstMissingFrag += 1
        elif header.fragNumber > self._firstMissingFrag:
            if (header.fragNumber - self._firstMissingFrag >
                MAX_STAGING_FRAGMENTS):
                debug("fragNumber too big")
            else:
                i = header.fragNumber - self._firstMissingFrag - 1
                payload, length = self._dataStagingRing[i]
                if payload is None:
                    self._dataStagingRing[i] = payloadCM.steal()
                else:
                    debug("duplicate fragment %d received" % header.fragNumber)
        else: # header.fragNumber < self._firstMissingFrag:
            # stale
            pass

        # TODO(ongaro): Have caller call self.sendAck() instead.
        if header.requestAck:
            self._sendAck()
        self._lastActivityTime = gettime()
        return (self._firstMissingFrag == self._totalFrags)

    def timeout(self):
        # Gratuitously ACK the received packets.
        self._sendAck()
        self._lastActivityTime = gettime()

class OutboundMessage(object):
    """A partially-transmitted data message (either a request or a response).

    This handles flow control and requesting and processing ACKs from the other
    end of the channel. It is used in server channels for the server's response
    and in client channels for the client's request.

    @ivar _transport: x
    @ivar _session: x
    @ivar _channelId: x
    @ivar _lastActivityTime: x
    @ivar _sendBuffer:
        The Buffer containing the message to send. This is set on the
        transition to SENDING and is None while IDLE.
    @ivar _totalFrags:
        The total number of fragments in the message to send.
    @ivar _firstMissingFrag:
        The number before which the receiving end has acknowledged receipt of
        every fragment, in the range [0, _totalFrags].
    @ivar _numAcked:
        The total number of fragments the receiving end has acknowledged, in
        the range [0, _totalFrags]. This is used for flow control, as the
        sender guarantees to send only fragments whose numbers are below
        _numAcked + WINDOW_SIZE.
    @ivar _sentTimes:
        A record of when unacknowledged fragments were sent, which is useful
        for retransmission.
        A Ring of MAX_STAGING_FRAGMENTS + 1 timestamps, where each entry
        corresponds with the time the _firstMissingFrag + i-th packet was sent
        (0 if it has never been sent), or _ACKED if it has already been
        acknowledged by the receiving end.
    @ivar _packetsSinceAckReq:
        The number of data packets sent on the wire since the last ACK request.
        This is used to determine when to request the next ACK.
    @ivar _state:
        Start in IDLE and move to SENDING once sending fragments of the message
        has begun. The clear() method will return to the IDLE state.

    @cvar _IDLE_STATE:
        There is no message to transmit currently.
    @cvar _SENDING_STATE:
        Transmission of the message has at least begun.
    @cvar _ACKED:
        A special value used in _sentTimes.
    """
    # TODO(ongaro): Can probably drop _state.

    _IDLE_STATE = 0
    _SENDING_STATE = 1

    _ACKED = object()

    def __init__(self, transport, session, channelId):
        self._transport = transport
        self._session = session
        self._channelId = channelId
        self._sentTimes = Ring(MAX_STAGING_FRAGMENTS + 1, 0)
        self.clear()

    def clear(self):
        self._state = self._IDLE_STATE
        self._sendBuffer = None
        self._firstMissingFrag = 0
        self._totalFrags = 0
        self._lastActivityTime = 0
        self._packetsSinceAckReq = 0
        self._sentTimes.clear()
        self._numAcked = 0

    def getLastActivityTime(self):
        return self._lastActivityTime

    def _sendOneData(self, fragNumber, forceRequestAck=False):
        """Send a single data fragment."""
        requestAck = (forceRequestAck or
                      (self._packetsSinceAckReq == REQ_ACK_AFTER - 1 and
                       fragNumber != self._totalFrags - 1))
        header = Header()
        self._session.fillHeader(header, self._channelId)
        header.fragNumber = fragNumber
        header.totalFrags = self._totalFrags
        header.requestAck = requestAck
        header.payloadType = Header.PT_DATA
        dataPerFragment = self._transport.dataPerFragment()
        payloadBuffer = Buffer([self._sendBuffer.getRange(fragNumber *
                                                          dataPerFragment,
                                                          dataPerFragment)])
        # TODO(ongaro): Driver sould take
        # (void *header, uint32_t headerLength,
        #  Buffer *payload, uint32_t payloadOffset, uint32_t payloadLength)
        # or
        # (void *header, uint32_t headerLength,
        #  BufferIterator *payloadFromOffsetThrough)
        self._transport._sendOne(self._session.getAddress(), header,
                                 payloadBuffer)
        if requestAck:
            self._packetsSinceAckReq = 0
        else:
            self._packetsSinceAckReq += 1

    def send(self):
        if self._state != self._SENDING_STATE:
            return

        now = gettime()

        # the number of fragments to be sent
        sendCount = 0

        # whether any of the fragments are being sent because they expired
        forceAck = False

        # the fragment number of the last fragment to be sent
        lastToSend = -1

        # can't send beyond the last fragment
        stop = self._totalFrags
        # can't send beyond the window
        stop = min(stop, self._numAcked + WINDOW_SIZE)
        # can't send beyond what the receiver is willing to accept
        stop = min(stop, self._firstMissingFrag + MAX_STAGING_FRAGMENTS + 1)

        # Figure out which fragments to send,
        # and flag them with _sentTimes of -1.
        for fragNumber in range(self._firstMissingFrag, stop):
            i = fragNumber - self._firstMissingFrag
            sentTime = self._sentTimes[i]
            if sentTime == 0:
                self._sentTimes[i] = -1
                sendCount += 1
                lastToSend = fragNumber
            elif sentTime is not self._ACKED and sentTime + TIMEOUT_NS < now:
                forceAck = True
                self._sentTimes[i] = -1
                sendCount += 1
                lastToSend = fragNumber

        forceAck = (forceAck and
                    (self._packetsSinceAckReq + sendCount < REQ_ACK_AFTER - 1
                     or lastToSend == self._totalFrags))

        # Send the fragments.
        for i, sentTime in enumerate(self._sentTimes):
            if sentTime == -1:
                fragNumber = self._firstMissingFrag + i
                self._sendOneData(fragNumber,
                                  (forceAck and lastToSend == fragNumber))

        # Update _sentTimes.
        now = gettime()
        self._lastActivityTime = now
        for i, sentTime in enumerate(self._sentTimes):
            if sentTime == -1:
                self._sentTimes[i] = now

    def beginSending(self, messageBuffer):
        # TODO(ongaro): Pass in the messageBuffer to clear() instead and rename
        # it (to "reset" or "reinit"?).
        """Start sending the message.

        This will send as many fragments of the message as is allowed by
        MAX_BURST_SIZE. ACKs from the other end will cause transmission to
        continue beyond that (see processReceivedAck() below).
        """
        assert self._state == self._IDLE_STATE
        self._state = self._SENDING_STATE
        self._sendBuffer = messageBuffer
        self._totalFrags = self._transport.numFrags(self._sendBuffer)

        # send out the first burst of fragments
        self.send()

    def processReceivedAck(self, ack):
        """
        Based on the information in an acknowledgement from the other side,
        send more packets. Tihs could be iether due to flow control or
        retransmission.
        """

        """Process an ACK response from the other end for the message.

        This will often free up send slots, allowing more fragments to be sent
        to the server.

        @param ack:
            An AckResponse object from the server for the request. It may
            acknowledge all packets, in which case this method won't send
            anything.
        @return:
            Whether all fragments have been acknowledged by the server.
        """
        if self._state != self._SENDING_STATE:
            debug("OutboundMessage droppped ack because not SENDING")
            return False

        if ack.firstMissingFrag < self._firstMissingFrag:
            debug("OutboundMessage dropped stale ACK")
        elif ack.firstMissingFrag > self._totalFrags:
            debug("OutboundMessage dropped invalid ACK (shouldn't happen)")
        elif ack.firstMissingFrag > (self._firstMissingFrag +
                                     len(self._sentTimes)):
            debug("OutboundMessage dropped ACK that advanced too far " +
                  "(shouldn't happen)")
        else:
            self._sentTimes.advance(ack.firstMissingFrag -
                                    self._firstMissingFrag)
            self._firstMissingFrag = ack.firstMissingFrag
            self._numAcked = ack.firstMissingFrag
            for i, acked in enumerate(ack.stagingVector.iterBits()):
                if acked:
                    self._sentTimes[i + 1] = self._ACKED
                    self._numAcked += 1
        self.send()
        return (self._firstMissingFrag == self._totalFrags)

    def timeout(self):
        self.send()

class Session(object):
    """A session encapsulates the state of communication between a particular
    client and a particular server.

    At the cost of a session open handshake (during which the server
    authenticates the client and allocates state for the client's session),
    sessions allow the client to open new channels for free. A channel is a
    connection within an established Session on which a sequence of RPCs
    travel.
    """

    def __init__(self, transport, sessionId):
        raise NotImplementedError

    def fillHeader(self, header, channelId):
        """Set Header fields according to this session and channel.

        This will set the rpcId, channelId, clientSessionHint,
        serverSessionHint, sessionToken, and direction.
        """
        raise NotImplementedError

    def getAddress(self):
        """Return the address of the node to which this Session
        communicates."""
        raise NotImplementedError

    def getLastActivityTime(self):
        raise NotImplementedError

    def expire(self):
        raise NotImplementedError

class ServerSession(Session):
    """A session on the server.

    @ivar _transport: x
    @ivar _lastActivityTime: x
    @ivar _id:
        The offset into the server's session table for this session.
    @ivar _channels:
        An array of ServerChannel objects of size NUM_CHANNELS_PER_SESSION.
    @ivar _token:
        A large integer that disambiguates this session from others before and
        after it on the same server with the same _id. None if IDLE.
    @ivar _address:
        The address of the client to which this session is connected.
        None if IDLE.
    @ivar _state:
        Start in IDLE, and startSession() moves from IDLE to ACTIVE. Then
        destroy() moves back to IDLE.

    @cvar _IDLE_STATE:
        Not connected to a client.
    @cvar _ACTIVE_STATE:
    """

    class _ServerChannel(object):
        """A channel on the server.

        @ivar rpcId:
            The current RPC ID that is being processed.
            None if IDLE, or an int RPC ID otherwise. This is set by advance().
        @ivar currentRpc:
            The current ServerRPC object active on this channel.
            None if IDLE or DISCARDED. Otherwise (if RECEIVING, PROCESSING, or
            SENDING_WAITING), a Transport.ServerRPC object that is dynamically
            allocated in processReceivedData() once the first data fragment of the
            request arrives.

            This could almost be allocated inline as part of the channel, but that
            has two problems. Firstly, _discard() currently orphans currentRpc
            if the server handler is currently processing it, which would need
            some other solution. Secondly, this would increase the size of idle
            channels to a few KB.
        @ivar inboundMsg:
            An InboundMessage to assemble the RPC request.
            None if IDLE or DISCARDED. Otherwise (if RECEIVING, PROCESSING, or
            SENDING_WAITING), an InboundMessage object that is dynamically
            allocated in _processReceivedData() once the first data fragment of
            the request arrives.

            This basically shares a lifetime with _currentRpc, so they could be
            allocated together.
        @ivar outboundMsg:
            An OutboundMessage to transmit the RPC response.
        @ivar state:
            Start at IDLE and move to RECEIVING once advance() assigns the channel an RPC ID.
            Move from RECEIVING to PROCESSING once the request is fully assembled
            in _processReceivedData(). Move from PROCESSING to DISCARDED if the
            handler ignored the request (rpcIgnored()) or to SENDING_WAITING if it
            produced a response (beginSending()). Move from SENDING_WAITING to
            DISCARDED if the client happened to ACK the entire response. At any
            time, destroy() moves back to IDLE.

            The server would also be free to discard the response after some period
            of time to reclaim space, but this is not currently implemented.

        @cvar IDLE_STATE:
            The channel is waiting to be assigned an RPC ID.
        @cvar RECEIVING_STATE:
            The channel has been assigned an RPC ID and is awaiting data fragments
            for it. Zero or more (but not all) such fragments have arrived.
        @cvar PROCESSING_STATE:
            The RPC is waiting in _transport._serverReadyQueue for processing or is
            being actively processed by the server handler.
        @cvar SENDING_WAITING_STATE:
            Still have the last RPC response.
        @cvar DISCARDED_STATE:
            Discarded the last RPC's data.
        """

        state = None
        rpcId = None
        currentRpc = None
        inboundMsg = None
        outboundMsg = None

        IDLE_STATE = 0
        RECEIVING_STATE = 1
        PROCESSING_STATE = 2
        SENDING_WAITING_STATE = 3
        DISCARDED_STATE = 4

    _IDLE_STATE = 0
    _ACTIVE_STATE = 1

    def _processReceivedData(self, channel, payloadCM):
        if channel.state == channel.IDLE_STATE:
            pass
        elif channel.state == channel.DISCARDED_STATE:
            header = Header()
            header.direction = Header.SERVER_TO_CLIENT
            header.clientSessionHint = self._clientSessionHint
            header.serverSessionHint = self._id
            header.sessionToken = self._token
            header.payloadType = PT_RETRY_WITH_NEW_RPCID
            self._transport._sendOne(self.getAddress(), header,
                                     Buffer([]))
        elif channel.state == channel.RECEIVING_STATE:
            isComplete = channel.inboundMsg.processReceivedData(payloadCM)
            if isComplete:
                self._transport._serverReadyQueue.append(channel.currentRpc)
                channel.state = channel.PROCESSING_STATE
        else: # PROCESSING or SENDING/WAITING
            header = Header.fromString(payloadCM.payload[:Header.LENGTH])
            if header.requestAck:
                channel.outboundMsg.send()

    def _processReceivedAck(self, channel, payloadCM):
        ack = AckResponse.fromString(payloadCM.payload[Header.LENGTH:])
        if channel.state != channel.SENDING_WAITING_STATE:
            return
        isCompletelyAcked = channel.outboundMsg.processReceivedAck(ack)
        if isCompletelyAcked:
            # Probably uncommon with this client implementation, but who knows?
            self._discard(channel)

    def _discard(self, channel):
        """Discard the channel's state except for the current rpcId."""
        if channel.state == channel.IDLE_STATE:
            return
        try:
            if channel.state == channel.PROCESSING_STATE:
                # TODO: Use a doubly-linked list with link pointers inside
                # channel.currentRpc instead of a Python "list" type. That'd
                # make this O(1).
                try:
                    q = self._transport._serverReadyQueue
                    del q[q.index(channel.currentRpc)]
                except ValueError:
                    # channel.currentRpc has already been popped off the queue,
                    # so there's no stopping the server from processing it now.
                    # We'll just tell it to abort once the server tries to send
                    # the reply.
                    channel.currentRpc.abort() # The RPC will delete() itself now.
                    channel.currentRpc = None
                    return
        finally:
            channel.state = channel.DISCARDED_STATE
            if channel.currentRpc is not None:
                delete(channel.currentRpc)
                channel.currentRpc = None
            channel.inboundMsg.init(None, None)
            channel.outboundMsg.clear()

    def __init__(self, transport, sessionId):
        self._transport = transport
        self._id = sessionId
        self._state = self._IDLE_STATE
        self._token = None
        self._lastActivityTime = 0
        self._address = None
        self._clientSessionHint = None
        self._channels = [self._ServerChannel()
                          for i in range(NUM_CHANNELS_PER_SESSION)]
        for channelId, channel in enumerate(self._channels):
            channel.state = channel.IDLE_STATE
            channel.rpcId = None
            channel.currentRpc = None
            # InboundMessage would be allocated as part of the channel
            channel.inboundMsg = InboundMessage(self._transport, self,
                                                channelId)
            # OutboundMessage would be allocated as part of the channel
            channel.outboundMsg = OutboundMessage(self._transport, self,
                                                  channelId)

    def processInboundPacket(self, payloadCM):
        header = Header.fromString(payloadCM.payload[:Header.LENGTH])
        if header.channelId >= NUM_CHANNELS_PER_SESSION:
            # Invalid channel. A well-behaved client wouldn't ever do this,
            # so it's safe to drop.
            debug("drop due to invalid channel")
            return

        channel = self._channels[header.channelId]
        if channel.rpcId is None:
            rpcIdIsOld = False
            rpcIdIsNew = True
        else:
            # TODO(ongaro): review modulo arithmetic
            rpcIdMask = (1 << RPCID_WIDTH) - 1
            diff = (header.rpcId - channel.rpcId) & rpcIdMask
            rpcIdIsOld = (diff >= 10 * 1000 * 1000)
            rpcIdIsNew = (0 < diff < 10 * 1000 * 1000)

        if rpcIdIsOld:
            # This must be an old packet that the client's no longer
            # waiting on, just drop it.
            debug("drop old packet")
        elif rpcIdIsNew:
            if header.payloadType == Header.PT_DATA:
                self._discard(channel)
                channel.rpcId = header.rpcId
                channel.state = channel.RECEIVING_STATE
                channel.currentRpc = new(Transport.ServerRPC(self._transport,
                                                             self,
                                                             header.channelId))
                requestBuffer = channel.currentRpc.recvPayload
                channel.inboundMsg.init(header.totalFrags, requestBuffer)
                self._processReceivedData(channel, payloadCM)
            else:
                # A well-behaved client wouldn't ever do this, so it's safe
                # to drop.
                debug("drop new rpcId with non-data")
        else: # header's RPC ID is same as channel's
            if header.payloadType == Header.PT_DATA:
                self._processReceivedData(channel, payloadCM)
            elif header.payloadType == Header.PT_ACK:
                self._processReceivedAck(channel, payloadCM)
            else:
                # A well-behaved client wouldn't ever do this, so it's safe
                # to drop.
                debug("drop current rpcId with bad type")

    def beginSending(self, channelId):
        """The server handler has finished producing the response; begin
        sending the response data."""
        channel = self._channels[channelId]
        assert channel.state == channel.PROCESSING_STATE
        channel.state = channel.SENDING_WAITING_STATE
        responseBuffer = channel.currentRpc.replyPayload
        channel.outboundMsg.beginSending(responseBuffer)

    def rpcIgnored(self, channelId):
        """The server handler chose to ignore this RPC request and will not
        produce a response for it."""
        channel = self._channels[channelId]
        assert channel.state == channel.PROCESSING_STATE
        self._discard(channel)

    def fillHeader(self, header, channelId):
        header.rpcId = self._channels[channelId].rpcId
        header.channelId = channelId
        header.direction = Header.SERVER_TO_CLIENT
        header.clientSessionHint = self._clientSessionHint
        header.serverSessionHint = self._id
        header.sessionToken = self._token

    def getToken(self):
        return self._token

    def getAddress(self):
        return self._address

    def startSession(self, address, clientSessionHint):
        assert self._state == self._IDLE_STATE
        self._state = self._ACTIVE_STATE
        self._address = address
        self._token = random.randrange(0, 1 << 64)
        self._clientSessionHint = clientSessionHint

        # send session open response
        header = Header()
        header.direction = Header.SERVER_TO_CLIENT
        header.clientSessionHint = self._clientSessionHint
        header.serverSessionHint = self._id
        header.sessionToken = self._token
        header.rpcId = 0
        header.channelId = 0
        header.payloadType = Header.PT_SESSION_OPEN
        payload = Buffer([])
        SessionOpenResponse(NUM_CHANNELS_PER_SESSION - 1).fillBuffer(payload)
        self._transport._sendOne(self._address, header, payload)
        self._lastActivityTime = gettime()

    def expire(self):
        if self._state == self._IDLE_STATE:
            return True
        for channel in self._channels:
            if channel.state != channel.IDLE_STATE:
                self._discard(channel)
                channel.state = channel.IDLE_STATE
                channel.rpcId = None

        for i, rpc in enumerate(self._transport._serverReadyQueue):
            if rpc._session == self:
                self._transport._serverReadyQueue[i] = None
                delete(rpc)
        self._transport._serverReadyQueue = filter(
            None, self._transport._serverReadyQueue)

        self._state = self._IDLE_STATE
        self._token = None
        self._clientSessionHint = None
        self._lastActivityTime = 0
        return True

    def getLastActivityTime(self):
        t = self._lastActivityTime
        if self._state == self._ACTIVE_STATE:
            for channel in self._channels:
                t = max(t, channel.inboundMsg.getLastActivityTime())
                t = max(t, channel.outboundMsg.getLastActivityTime())
        return t

class ClientSession(Session):
    """A session on the client."""

    class _ClientChannel(object):
        """A channel on the client.

        @ivar rpcId:
            The RPC Id for next packet if IDLE or the active one if non-IDLE.
        @ivar currentRpc:
            Pointer to external Transport.ClientRPC if non-IDLE. None if IDLE.
        @ivar numRetries:
            The number of ACK requests or responses we've sent due to timeouts
            since the last time the server has responded. This is cleared on the
            receipt of response data or an ACK for request data.
        """

        # start at IDLE.
        # destroy() transitions to IDLE.
        # beginSending() transitions from IDLE to SENDING.
        # processReceivedData() transitions from SENDING to RECEIVING.
        # retryWithNewRpcId() transitions from non-IDLE to SENDING.
        state = None
        rpcId = None
        currentRpc = None
        outboundMsg = None
        inboundMsg = None
        numRetries = None

        IDLE_STATE = 0
        SENDING_STATE = 1
        RECEIVING_STATE = 2

    def _isConnected(self):
        return self._token is not None

    def _sendSessionOpenRequest(self):
        header = Header()
        header.direction = Header.CLIENT_TO_SERVER
        header.clientSessionHint = self._id
        header.serverSessionHint = self._serverSessionHint
        header.sessionToken = self._token
        header.rpcId = 0
        header.serverSessionHint = 0
        header.sessionToken = 0
        header.channelId = 0
        header.payloadType = Header.PT_SESSION_OPEN
        self._transport._sendOne(self.getAddress(), header, Buffer([]))
        # TODO(ongaro): Would it be possible to open a session like other RPCs?
        # TODO: set up timer
        self._lastActivityTime = gettime()

    def _processSessionOpenResponse(self, payloadCM):
        """Process an inbound session open response."""
        if self._isConnected():
            return
        header = Header.fromString(payloadCM.payload[:Header.LENGTH])
        d = payloadCM.payload[Header.LENGTH:]
        response = SessionOpenResponse.fromString(d)
        self._serverSessionHint = header.serverSessionHint
        self._token = header.sessionToken
        self._numChannels = min(response.maxChannelId + 1,
                                MAX_NUM_CHANNELS_PER_SESSION)

        self._channels = new([self._ClientChannel()
                              for i in range(self._numChannels)])
        for channelId, channel in enumerate(self._channels):
            channel.state = channel.IDLE_STATE
            channel.rpcId = 0
            channel.currentRpc = None
            # OutboundMessage would be allocated as part of the channel
            channel.outboundMsg = OutboundMessage(self._transport, self,
                                                  channelId)
            # InboundMessage would be allocated as part of the channel
            channel.inboundMsg = InboundMessage(self._transport, self,
                                                channelId)
            channel.numRetries = 0
        for channelId, channel in enumerate(self._channels):
            try:
                rpc = self._channelQueue.pop(0)
            except IndexError:
                break
            else:
                self._channelStatus.setBit(channelId)
                channel.state = channel.SENDING_STATE
                channel.currentRpc = rpc
                channel.outboundMsg.beginSending(rpc.getRequestBuffer())

    def _processReceivedData(self, channel, payloadCM):
        if channel.state == channel.IDLE_STATE:
            return
        header = Header.fromString(payloadCM.payload[:Header.LENGTH])
        channel.numRetries = 0
        if channel.state == channel.SENDING_STATE:
            responseBuffer = channel.currentRpc.getResponseBuffer()
            channel.inboundMsg.init(header.totalFrags, responseBuffer)
            channel.state = channel.RECEIVING_STATE
        if channel.inboundMsg.processReceivedData(payloadCM):
            channel.currentRpc.completed()
            channel.state = channel.IDLE_STATE
            channel.rpcId = (channel.rpcId + 1) % (1 << RPCID_WIDTH)
            channel.currentRpc = None
            channel.outboundMsg.clear()
            channel.inboundMsg.init(None, None)
            channel.numRetries = 0

            self._doneWithChannel(channel)

    def _doneWithChannel(self, channel):
        """Mark a channel as available.

        If there's an RPC waiting on an available channel, it will be started.

        This method should only be called by self and one of self._channels.
        """
        # TODO(ongaro): Rename this.
        # TODO(ongaro): Maybe pass in a channelId.
        channelId = self._channels.index(channel)
        assert self._channelStatus.getBit(channelId)
        try:
            rpc = self._channelQueue.pop(0)
        except IndexError:
            self._channelStatus.clearBit(channelId)
        else:
            assert channel.state == channel.IDLE_STATE
            channel.state = channel.SENDING_STATE
            channel.currentRpc = rpc
            channel.outboundMsg.beginSending(rpc.getRequestBuffer())

    def _processReceivedAck(self, channel, payloadCM):
        if channel.state != channel.SENDING_STATE:
            return
        ack = AckResponse.fromString(payloadCM.payload[Header.LENGTH:])
        channel.numRetries = 0
        channel.outboundMsg.processReceivedAck(ack)

    def _retryWithNewRpcId(self, channel):
        channel.state = channel.SENDING_STATE
        channel.rpcId = (channel.rpcId + 1) % (1 << RPCID_WIDTH)

        channel.outboundMsg.clear()
        channel.inboundMsg.init(None, None)

        channel.numRetries = 0

        channel.outboundMsg.beginSending(channel.currentRpc.getRequestBuffer())

    def _getAvailableChannel(self):
        """Return any available ClientChannel object or None."""
        if not self._isConnected():
            return None
        channelId = self._channelStatus.ffz()
        if channelId is None or channelId >= self._numChannels:
            return None
        self._channelStatus.setBit(channelId)
        return self._channels[channelId]

    def _clearChannels(self):
        for i in range(MAX_NUM_CHANNELS_PER_SESSION):
            self._channelStatus.clearBit(i)
        self._numChannels = 0
        if self._channels is not None:
            delete(self._channels)
        self._channels = None

    def __init__(self, transport, sessionId):
        self._transport = transport
        self._id = sessionId
        self._service = None
        self._channelQueue = []

        # A bit set in the vector signifies the corresponding channel is in
        # use. Starts out as all 0s.
        # TODO: Remove this in favor of looking at the Channel objects.
        self._channelStatus = BitVector(MAX_NUM_CHANNELS_PER_SESSION)

        self._numChannels = 0
        self._channels = None
        self._serverSessionHint = None
        self._token = None
        self._lastActivityTime = 0

    def connect(self, service):
        self._service = service
        self._sendSessionOpenRequest()
        # TODO(ongaro): Would it be safe to call poll here and wait?

    def fillHeader(self, header, channelId):
        header.direction = Header.CLIENT_TO_SERVER
        header.clientSessionHint = self._id
        header.serverSessionHint = self._serverSessionHint
        header.sessionToken = self._token
        header.channelId = channelId
        header.rpcId = self._channels[channelId].rpcId

    def getAddress(self):
        return self._service.address

    def processInboundPacket(self, payloadCM):
        """Return whether the session is still valid."""

        self._lastActivityTime = gettime()

        header = Header.fromString(payloadCM.payload[:Header.LENGTH])

        if header.channelId >= self._numChannels:
            if header.payloadType == Header.PT_SESSION_OPEN:
                self._processSessionOpenResponse(payloadCM)
            return True

        channel = self._channels[header.channelId]
        if channel.rpcId == header.rpcId:
            if header.payloadType == Header.PT_DATA:
                self._processReceivedData(channel, payloadCM)
            elif header.payloadType == Header.PT_ACK:
                self._processReceivedAck(channel, payloadCM)
            elif header.payloadType == Header.PT_SESSION_OPEN:
                # The session is already open, so just drop this.
                pass
            elif header.payloadType == Header.PT_BAD_SESSION:
                for channel in self._channels:
                    if channel.currentRpc is not None:
                        self._channelQueue.append(channel.currentRpc)
                self._clearChannels()
                self._serverSessionHint = None
                self._token = None
                self.connect()
                return False
            elif header.payloadType == Header.PT_RETRY_WITH_NEW_RPCID:
                self._retryWithNewRpcId(channel)
        else:
            if (0 < channel.rpcId - header.rpcId < 1024 and
                header.payloadType == Header.PT_DATA and header.requestAck):
                raise NotImplementedError("faked full ACK response")
        return True

    def startRpc(self, rpc):
        """Queue an RPC for transmission on this session.

        This session will try its best to send the RPC.
        """
        # TODO(ongaro): What if finding a new session isn't going to do it,
        # because the server has crashed?
        channel = self._getAvailableChannel()
        if channel is None:
            # TODO(ongaro): Thread linked list through rpc.
            self._channelQueue.append(rpc)
        else:
            assert channel.state == channel.IDLE_STATE
            channel.state = channel.SENDING_STATE
            channel.currentRpc = rpc
            channel.outboundMsg.beginSending(rpc.getRequestBuffer())

    def getActiveChannels(self):
        if not self._isConnected():
            return
        for channelId, isActive in enumerate(self._channelStatus.iterBits()):
            if isActive and channelId < self._numChannels:
                yield channelId

    def getLastActivityTime(self, channelId=None):
        t = 0
        if channelId is not None:
            channel = self._channels[channelId]
            t = max(t, channel.outboundMsg.getLastActivityTime())
            t = max(t, channel.inboundMsg.getLastActivityTime())
        else:
            t = max(t, self._lastActivityTime)
            if self._channels is not None:
                for channel in self._channels:
                    t = max(t, channel.outboundMsg.getLastActivityTime())
                    t = max(t, channel.inboundMsg.getLastActivityTime())
        return t

    def timeout(self, channelId):
        """Called by _checkTimers."""
        channel = self._channels[channelId]
        channel.numRetries += 1
        if channel.numRetries == TIMEOUTS_UNTIL_ABORTING:
            debug("Aborting after %d timeouts" % TIMEOUTS_TILL_ABORTING)
            for channel in self._channels:
                if channel.currentRpc is not None:
                    channel.currentRpc.aborted()
            for rpc in self._channelQueue:
                rpc.aborted()
            self._channelQueue = []
            self._clearChannels()
            self._serverSessionHint = None
            self._token = None
            return
        if channel.state == channel.SENDING_STATE:
            channel.outboundMsg.timeout()
        elif channel.state == channel.RECEIVING_STATE:
            channel.inboundMsg.timeout()
        else:
            pass

    def expire(self):
        for channel in self._channels:
            if channel.currentRpc is not None:
                return False
        if len(self._channelQueue) > 0:
            return False
        self._clearChannels()
        self._serverSessionHint = None
        self._token = None
        return True

class SessionTable(object):
    def __init__(self, transport, sessionClass):
        self._transport = transport
        self._sessionClass = sessionClass
        self._sessions = []
        self._available = []
        self._lastCleanedIndex = 0

    def __getitem__(self, sessionId):
        return self._sessions[sessionId]

    def __len__(self):
        return len(self._sessions)

    def get(self):
        try:
            session = self._available.pop()
        except IndexError:
            sessionId = len(self._sessions)
            session = new(self._sessionClass(self._transport, sessionId))
            self._sessions.append(session)
        return session

    def put(self, session):
        self._available.append(session)

    def expire(self, sessionsToCheck=5):
        now = gettime()
        for j in range(sessionsToCheck):
            self._lastCleanedIndex += 1
            if self._lastCleanedIndex >= len(self._sessions):
                self._lastCleanedIndex = 0
                if len(self._sessions) == 0:
                    break
            session = self._sessions[self._lastCleanedIndex]
            if (session not in self._available and
                session.getLastActivityTime() + SESSION_TIMEOUT_NS < now):
                if session.expire():
                    self.put(session)

class Transport(object):
    class TransportException(Exception):
        pass

    def __init__(self, driver, isServer):
        self._driver = driver
        self._isServer = isServer

        if self._isServer:
            self._serverSessions = SessionTable(self, ServerSession)

            # a list of dynamically allocated Transport.ServerRPC objects that
            # are ready for processing by server handlers
            self._serverReadyQueue = []

        self._clientSessions = SessionTable(self, ClientSession)

    def dataPerFragment(self):
        return (self._driver.MAX_PAYLOAD_SIZE - Header.LENGTH)

    def numFrags(self, dataBuffer):
        return ((dataBuffer.getTotalLength() + self.dataPerFragment() - 1) /
                self.dataPerFragment())

    def _sendOne(self, address, header, dataBuffer):
        """Dump a packet onto the wire."""
        assert header.fragNumber < header.totalFrags
        header.pleaseDrop = (random.random() < PACKET_LOSS)
        dataBuffer.prepend(str(header))
        # TODO(ongaro): Will a sync API to Driver allow us to fully utilize
        # the NIC?
        self._driver.sendPacket(address, dataBuffer)

    def _checkTimers(self):
        now = gettime()

        # TODO(ongaro): This won't scale well to clients that have a large
        # number of sessions open. Maybe a linked list through the active
        # sessions instead?
        for session in self._clientSessions:
            for channelId in session.getActiveChannels():
                if session.getLastActivityTime(channelId) + TIMEOUT_NS < now:
                    session.timeout(channelId)

        # server role:
        # If the client hasn't received our entire response, that's their
        # problem. No need to handle timeouts here.
        # TODO(ongaro): periodic cleanup of state?

    def _checkWire(self):
        x = self._driver.tryRecvPacket()
        if x is None:
            return False

        payload, length, address = x
        with PayloadReleaser(self._driver, payload, length) as payloadCM:
            if Header.LENGTH > length:
                debug("packet too small")
                return True
            header = Header.fromString(payload[:Header.LENGTH])
            if header.pleaseDrop:
                return True

            if header.direction == header.CLIENT_TO_SERVER:
                if not self._isServer:
                    # This must be an old or malformed packet,
                    # so it is safe to drop.
                    debug("drop -- not a server")
                    return True
                if header.serverSessionHint < len(self._serverSessions):
                    session = self._serverSessions[header.serverSessionHint]
                    if session.getToken() == header.sessionToken:
                        session.processInboundPacket(payloadCM)
                        return True
                if header.payloadType == Header.PT_SESSION_OPEN:
                    self._serverSessions.expire()
                    session = self._serverSessions.get()
                    session.startSession(address, header.clientSessionHint)
                else:
                    replyHeader = Header()
                    replyHeader.sessionToken = header.sessionToken
                    replyHeader.rpcId = header.rpcId
                    replyHeader.clientSessionHint = header.clientSessionHint
                    replyHeader.serverSessionHint = header.serverSessionHint
                    replyHeader.channelId = header.channelId
                    replyHeader.payloadType = Header.PT_BAD_SESSION
                    replyHeader.direction = Header.SERVER_TO_CLIENT
                    self._sendOne(address, replyHeader, Buffer([]))
            else:
                if header.clientSessionHint < len(self._clientSessions):
                    session = self._clientSessions[header.clientSessionHint]
                    stillValid = session.processInboundPacket(payloadCM)
                    if not stillValid:
                        self._clientSessions.put(session)
        return True

    def poll(self):
        """Check the wire and check timers. Do all possible work but don't
        wait."""

        while self._checkWire(): # TODO(ongaro): rename from "check"
            self._checkTimers()
        self._checkTimers()

    def getClientSession(self):
        self._clientSessions.expire()
        return self._clientSessions.get()

    class ClientRPC(object):
        _IN_PROGRESS_STATE = 0
        _COMPLETED_STATE = 1
        _ABORTED_STATE = 2

        def __init__(self, transport, service, requestBuffer, responseBuffer):
            self._transport = transport
            self._service = service

            # pointers to buffers on client's stack
            self._requestBuffer = requestBuffer
            self._responseBuffer = responseBuffer

            self._state = self._IN_PROGRESS_STATE
            self._service.session.startRpc(self)

        def getRequestBuffer(self):
            return self._requestBuffer

        def getResponseBuffer(self):
            return self._responseBuffer

        def aborted(self):
            self._state = self._ABORTED_STATE

        def completed(self):
            """A callback for when the response Buffer has been filled with the
            response."""
            self._state = self._COMPLETED_STATE

        def getReply(self):
            try:
                while True:
                    if self._state == self._COMPLETED_STATE:
                        return
                    elif self._state == self._ABORTED_STATE:
                        raise self._transport.TransportException("RPC aborted")
                    self._transport.poll()
            finally:
                delete(self)

    def clientSend(self, service, requestBuffer, responseBuffer):
        # TODO(ongaro): Allocate the ClientRPC in requestBuffer or responseBuffer?
        rpc = new(Transport.ClientRPC(self, service, requestBuffer,
                                      responseBuffer))
        # TODO: Move the work out of the constructor to an rpc.start() or
        # similar
        return rpc

    class ServerRPC(object):
        _PROCESSING_STATE = 0
        _COMPLETED_STATE = 1
        _ABORTED_STATE = 2

        def __init__(self, transport, session, channelId):
            self._state = self._PROCESSING_STATE
            self._transport = transport
            self._session = session
            self._channelId = channelId
            self.recvPayload = Buffer()
            self.replyPayload = Buffer()

        def sendReply(self):
            if self._state == self._ABORTED_STATE:
                delete(self)
            elif self._state == self._PROCESSING_STATE:
                self._state = self._COMPLETED_STATE
                self._session.beginSending(self._channelId)
                # TODO: don't forget to delete(self) eventually
            else:
                assert False

        def ignore(self):
            if self._state == self._ABORTED_STATE:
                delete(self)
            elif self._state == self._PROCESSING_STATE:
                self._state = self._COMPLETED_STATE
                self._session.rpcIgnored(self._channelId)
            else:
                assert False

        def abort(self):
            """This object takes responsibility for deleting itself."""
            self._state = self._ABORTED_STATE
            self._transport = None
            self._session = None
            self._channelId = None

    def serverRecv(self):
        while True:
            self.poll()
            try:
                return self._serverReadyQueue.pop(0)
            except IndexError:
                pass

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

interface IAgentIdentity {
    function ownerOf(uint256 agentId) external view returns (address);
}

contract PaymentChannel is EIP712, ReentrancyGuard {

    enum Status {None, Open, Closing, Closed}

    struct Channel {
        address payer;
        Status status;
        uint64 challengePeriod;
        uint64 closeDeadline;
        uint256 agentId;
        uint256 deposit;
        uint256 claimed; // highest valid cumulativeAmount submitted during a unilateral close
    }

    struct Voucher {
        bytes32 channelId;
        uint256 agentId;
        uint256 cumulativeAmount;
        uint64 nonce;
        uint64 expiry;
        bytes32 requestHash;
    }


    bytes32 public constant VOUCHER_TYPEHASH =
        keccak256(
            "Voucher(bytes32 channelId,uint256 agentId,uint256 cumulativeAmount,uint64 nonce,uint64 expiry,bytes32 requestHash)"
        );

    uint64 public constant MAX_CHALLENGE_PERIOD = 30 days;

    IAgentIdentity public immutable identity;
    uint64 public immutable minChallengePeriod;

    uint256 public channelCount;
    mapping(bytes32 => Channel) private _channels;
    mapping(address => uint256) public pending;


    event ChannelOpened(
        bytes32 indexed channelId,
        address indexed payer,
        uint256 indexed agentId,
        uint256 deposit,
        uint64 challengePeriod
    );
    event ChannelToppedUp(bytes32 indexed channelId, uint256 amount, uint256 newDeposit);
    event CloseStarted(bytes32 indexed channelId, uint64 deadline);
    event VoucherSubmitted(bytes32 indexed channelId, uint256 cumulativeAmount, uint64 nonce);
    event ChannelClosed(bytes32 indexed channelId, address indexed payee, uint256 payeeAmount, uint256 payerRefund);
    event Withdrawn(address indexed account, uint256 amount);


    error ZeroDeposit();
    error InvalidChallengePeriod(uint64 provided);
    error UnknownAgent(uint256 agentId);
    error UnknownChannel(bytes32 channelId);
    error NotPayer();
    error NotPayee();
    error ChannelNotOpen();
    error ChannelNotClosing();
    error ChannelNotActive();
    error ChallengeWindowOpen(uint64 deadline);
    error ChallengeWindowOver(uint64 deadline);
    error VoucherMismatch();
    error InvalidSignature();
    error NothingToWithdraw();
    error TransferFailed();

    constructor(address identityRegistry, uint64 minChallengePeriod_) EIP712("AMXPaymentChannel", "1") {
        require(identityRegistry != address(0), "identity=0");
        require(minChallengePeriod_ <= MAX_CHALLENGE_PERIOD, "min>max");
        identity = IAgentIdentity(identityRegistry);
        minChallengePeriod = minChallengePeriod_;
    }


    function openChannel(uint256 agentId, uint64 challengePeriod) external payable returns (bytes32 channelId) {
        if (msg.value == 0) revert ZeroDeposit();
        if (challengePeriod < minChallengePeriod || challengePeriod > MAX_CHALLENGE_PERIOD) {
            revert InvalidChallengePeriod(challengePeriod);
        }
        try identity.ownerOf(agentId) returns (address) {} catch {
            revert UnknownAgent(agentId);
        }

        channelCount += 1;
        channelId = keccak256(abi.encode(block.chainid, address(this), msg.sender, agentId, channelCount));

        _channels[channelId] = Channel({
            payer: msg.sender,
            status: Status.Open,
            challengePeriod: challengePeriod,
            closeDeadline: 0,
            agentId: agentId,
            deposit: msg.value,
            claimed: 0
        });

        emit ChannelOpened(channelId, msg.sender, agentId, msg.value, challengePeriod);
    }

    function topUp(bytes32 channelId) external payable {
        Channel storage ch = _channels[channelId];
        if (ch.status != Status.Open) revert ChannelNotOpen();
        if (msg.sender != ch.payer) revert NotPayer();
        if (msg.value == 0) revert ZeroDeposit();

        ch.deposit += msg.value;
        emit ChannelToppedUp(channelId, msg.value, ch.deposit);
    }


    function close(Voucher calldata v, bytes calldata signature) external nonReentrant {
        Channel storage ch = _channels[v.channelId];
        _requireActive(ch, v.channelId);
        if (ch.status == Status.Closing && block.timestamp >= ch.closeDeadline) {
            revert ChallengeWindowOver(ch.closeDeadline);
        }

        address payee = identity.ownerOf(ch.agentId);
        if (msg.sender != payee) revert NotPayee();

        _verifyVoucher(ch, v, signature);

        uint256 owed = v.cumulativeAmount > ch.claimed ? v.cumulativeAmount : ch.claimed;
        _settle(v.channelId, ch, payee, owed);
    }


    function startClose(bytes32 channelId) external {
        Channel storage ch = _channels[channelId];
        if (ch.status == Status.None) revert UnknownChannel(channelId);
        if (ch.status != Status.Open) revert ChannelNotOpen();
        if (msg.sender != ch.payer) revert NotPayer();

        ch.status = Status.Closing;
        ch.closeDeadline = uint64(block.timestamp) + ch.challengePeriod;

        emit CloseStarted(channelId, ch.closeDeadline);
    }

    function submitVoucher(Voucher calldata v, bytes calldata signature) external {
        Channel storage ch = _channels[v.channelId];
        if (ch.status == Status.None) revert UnknownChannel(v.channelId);
        if (ch.status != Status.Closing) revert ChannelNotClosing();
        if (block.timestamp >= ch.closeDeadline) revert ChallengeWindowOver(ch.closeDeadline);

        _verifyVoucher(ch, v, signature);

        if (v.cumulativeAmount > ch.claimed) {
            ch.claimed = v.cumulativeAmount;
        }
        emit VoucherSubmitted(v.channelId, v.cumulativeAmount, v.nonce);
    }

    function finalizeClose(bytes32 channelId) external nonReentrant {
        Channel storage ch = _channels[channelId];
        if (ch.status == Status.None) revert UnknownChannel(channelId);
        if (ch.status != Status.Closing) revert ChannelNotClosing();
        if (block.timestamp < ch.closeDeadline) revert ChallengeWindowOpen(ch.closeDeadline);

        address payee = identity.ownerOf(ch.agentId);
        _settle(channelId, ch, payee, ch.claimed);
    }


    function withdraw() external nonReentrant {
        uint256 amount = pending[msg.sender];
        if (amount == 0) revert NothingToWithdraw();
        pending[msg.sender] = 0;

        (bool ok, ) = msg.sender.call{value: amount}("");
        if (!ok) revert TransferFailed();

        emit Withdrawn(msg.sender, amount);
    }

    function getChannel(bytes32 channelId) external view returns (Channel memory) {
        return _channels[channelId];
    }

    function voucherDigest(Voucher calldata v) external view returns (bytes32) {
        return _hashVoucher(v);
    }

    function domainSeparator() external view returns (bytes32) {
        return _domainSeparatorV4();
    }


    function _requireActive(Channel storage ch, bytes32 channelId) internal view {
        if (ch.status == Status.None) revert UnknownChannel(channelId);
        if (ch.status != Status.Open && ch.status != Status.Closing) revert ChannelNotActive();
    }

    function _hashVoucher(Voucher calldata v) internal view returns (bytes32) {
        return
            _hashTypedDataV4(
                keccak256(
                    abi.encode(
                        VOUCHER_TYPEHASH,
                        v.channelId,
                        v.agentId,
                        v.cumulativeAmount,
                        v.nonce,
                        v.expiry,
                        v.requestHash
                    )
                )
            );
    }

    function _verifyVoucher(Channel storage ch, Voucher calldata v, bytes calldata signature) internal view {
        if (v.agentId != ch.agentId) revert VoucherMismatch();
        address signer = ECDSA.recover(_hashVoucher(v), signature);
        if (signer != ch.payer) revert InvalidSignature();
    }

    function _settle(bytes32 channelId, Channel storage ch, address payee, uint256 owed) internal {
        uint256 payeeAmount = owed > ch.deposit ? ch.deposit : owed;
        uint256 refund = ch.deposit - payeeAmount;

        ch.status = Status.Closed;

        if (payeeAmount != 0) pending[payee] += payeeAmount;
        if (refund != 0) pending[ch.payer] += refund;

        emit ChannelClosed(channelId, payee, payeeAmount, refund);
    }
}

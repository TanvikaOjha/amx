//SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC721URIStorage, ERC721} "@openzeppelin/contracts/token/ERC721/extensions/ERC721URIStorage.sol";

contract  ERC8004Identity is ERC721URIStorage {
    struct MetadataEntry {
        string metadataKey;
        bytes metadataValue;
    }
    struct Feedback {
        uint8 score; 
        bool exists;
    }

    string public constant PUBKEY_KEY = "pubkey";
    uint8 public constant MAX_SCORE = 100;
    uint256 public constant MAX_TAG_LENGTH= 64;
    uint256 public constant PUBKEY_LENGTH = 64;

    uint private _lastAgentId;
     //agentId => key => value
    mapping(uint256 => mapping(string => bytes)) private _metadata;

    //reputation: agentId => client => latestFeedback
    mapping(uint256 => mapping(address => Feedback)) private _feedback;
    mapping(uint256 => uint256) private _scoreSum;

    event Registered(uint256 indexed agentId, string agentURI, address indexed owner);
    event URIUpdated(uint256 indexed agentId, string newURI, address indexed updatedBy);
    event MetadataSet(uint256 indexed agentId, string indexed indexedMetadataKey, string metadataKey, bytes metadataValue);

    event FeedbackGiven(uint256 indexed agentId, address indexed client, uint8 score, string tag);

     error EmptyMetadataKey();
    error InvalidPubKey();
    error ScoreOutOfRange(uint8 score);
    error TagTooLong(uint256 length);
    error SelfFeedbackNotAllowed();

    constructor() ERC721("AMX Agent Identity", "AMX-AGENT") {}

     function register() external returns (uint256 agentId) {
        return _register("", new MetadataEntry[](0));
    }
    function register(string calldata agentURI_) external returns (uint256 agentId) {
        return _register(agentURI_, new MetadataEntry[](0));
    }
     function register(
        string calldata agentURI_,
        MetadataEntry[] calldata metadata
    ) external returns (uint256 agentId) {
        return _register(agentURI_, metadata);
    }

    function _register(
        string memory agentURI_,
        MetadataEntry[] memory metadata
    ) internal returns (uint256 agentId) {
        agentId = ++_lastAgentId;

        // Effects first, then the (potentially re-entrant) safe mint.
        if (bytes(agentURI_).length != 0) {
            _setTokenURI(agentId, agentURI_);
        }
        for (uint256 i = 0; i < metadata.length; i++) {
            _writeMetadata(agentId, metadata[i].metadataKey, metadata[i].metadataValue);
        }

        _safeMint(msg.sender, agentId);
        emit Registered(agentId, agentURI_, msg.sender);
    }

    function setAgentURI(uint256 agentId, string calldata newURI) external {
        _requireAuthorizedFor(agentId);
        _setTokenURI(agentId, newURI);
        emit URIUpdated(agentId, newURI, msg.sender);
    }

    function setMetadata(uint256 agentId, string calldata metadataKey, bytes calldata metadataValue) external {
        _requireAuthorizedFor(agentId);
        _writeMetadata(agentId, metadataKey, metadataValue);
    }

    function getMetadata(uint256 agentId, string calldata metadataKey) external view returns (bytes memory) {
        _requireOwned(agentId);
        return _metadata[agentId][metadataKey];
    }
   function agentURI(uint256 agentId) external view returns (string memory) {
        return tokenURI(agentId);
    }
    function pubKey(uint256 agentId) public view returns (bytes memory) {
        _requireOwned(agentId);
        return _metadata[agentId][PUBKEY_KEY];
    }
    function signerOf(uint256 agentId) external view returns (address) {
        bytes memory key = pubKey(agentId);
        if (key.length != PUBKEY_LENGTH) return address(0);
        return address(uint160(uint256(keccak256(key))));
    }

    function totalAgents() external view returns (uint256) {
        return _lastAgentId;
    }
    function giveFeedback(uint256 agentId, uint8 score, string calldata tag) external {
        address owner_ = _requireOwned(agentId);
        if (score > MAX_SCORE) revert ScoreOutOfRange(score);
        if (bytes(tag).length > MAX_TAG_LENGTH) revert TagTooLong(bytes(tag).length);
        if (_isAuthorized(owner_, msg.sender, agentId)) revert SelfFeedbackNotAllowed();

        Feedback storage fb = _feedback[agentId][msg.sender];
        if (fb.exists) {
            _scoreSum[agentId] = _scoreSum[agentId] - fb.score + score;
        } else {
            _scoreSum[agentId] += score;
            _feedbackCount[agentId] += 1;
            fb.exists = true;
        }
        fb.score = score;

        emit FeedbackGiven(agentId, msg.sender, score, tag);
    }

    function getSummary(uint256 agentId) external view returns (uint256 clientCount, uint256 scoreSum) {
        _requireOwned(agentId);
        return (_feedbackCount[agentId], _scoreSum[agentId]);
    }

    function getFeedback(uint256 agentId, address client) external view returns (uint8 score, bool exists) {
        _requireOwned(agentId);
        Feedback storage fb = _feedback[agentId][client];
        return (fb.score, fb.exists);
    }


    function _requireAuthorizedFor(uint256 agentId) internal view {
        address owner_ = _requireOwned(agentId);
        _checkAuthorized(owner_, msg.sender, agentId);
    }

    function _writeMetadata(uint256 agentId, string memory key, bytes memory value) internal {
        if (bytes(key).length == 0) revert EmptyMetadataKey();
        if (
            keccak256(bytes(key)) == keccak256(bytes(PUBKEY_KEY)) &&
            value.length != 0 &&
            value.length != PUBKEY_LENGTH
        ) {
            revert InvalidPubKey();
        }
        _metadata[agentId][key] = value;
        emit MetadataSet(agentId, key, key, value);
    }

    function _update(address to, uint256 tokenId, address auth) internal override returns (address from) {
        from = super._update(to, tokenId, auth);
        if (from != address(0) && to != address(0) && from != to) {
            if (_metadata[tokenId][PUBKEY_KEY].length != 0) {
                delete _metadata[tokenId][PUBKEY_KEY];
                emit MetadataSet(tokenId, PUBKEY_KEY, PUBKEY_KEY, "");
            }
        }
    }
}
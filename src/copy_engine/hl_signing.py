"""Verified Hyperliquid L1 action signing primitives.

These functions mirror the official hyperliquid-python-sdk signing flow.  The
application keeps them under its own module because the project already has a
local ``hyperliquid`` package for its REST/WebSocket models.

Adapted from hyperliquid-python-sdk 0.24.0, distributed under the MIT license.
"""

from decimal import Decimal
from typing import Any, Dict, Optional
import time

import msgpack
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_hex


def _address_to_bytes(address: str) -> bytes:
    return bytes.fromhex(address[2:] if address.startswith("0x") else address)


def _action_hash(
    action: Dict[str, Any],
    vault_address: Optional[str],
    nonce: int,
    expires_after: Optional[int],
) -> bytes:
    data = msgpack.packb(action)
    data += nonce.to_bytes(8, "big")
    if vault_address is None:
        data += b"\x00"
    else:
        data += b"\x01"
        data += _address_to_bytes(vault_address)
    if expires_after is not None:
        data += b"\x00"
        data += expires_after.to_bytes(8, "big")
    return keccak(data)


def _l1_payload(phantom_agent: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "domain": {
            "chainId": 1337,
            "name": "Exchange",
            "verifyingContract": "0x0000000000000000000000000000000000000000",
            "version": "1",
        },
        "types": {
            "Agent": [
                {"name": "source", "type": "string"},
                {"name": "connectionId", "type": "bytes32"},
            ],
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
        },
        "primaryType": "Agent",
        "message": phantom_agent,
    }


def sign_l1_action(
    wallet: Any,
    action: Dict[str, Any],
    vault_address: Optional[str],
    nonce: int,
    expires_after: Optional[int],
    is_mainnet: bool,
) -> Dict[str, Any]:
    """Sign an exchange action using Hyperliquid's official L1 scheme."""
    action_digest = _action_hash(action, vault_address, nonce, expires_after)
    phantom_agent = {
        "source": "a" if is_mainnet else "b",
        "connectionId": action_digest,
    }
    signed = wallet.sign_message(encode_typed_data(full_message=_l1_payload(phantom_agent)))
    return {"r": to_hex(signed["r"]), "s": to_hex(signed["s"]), "v": signed["v"]}


def float_to_wire(value: Any) -> str:
    """Use the SDK's wire representation and reject lossy rounding."""
    value = float(value)
    rounded = f"{value:.8f}"
    if abs(float(rounded) - value) >= 1e-12:
        raise ValueError(f"float_to_wire causes rounding: {value}")
    if rounded == "-0":
        rounded = "0"
    return f"{Decimal(rounded).normalize():f}"


def get_timestamp_ms() -> int:
    return int(time.time() * 1000)

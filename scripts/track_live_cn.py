"""One-off live consignment tracking verification script."""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)

from aftership_client import create_tracking, find_tracking_id, get_tracking
from database.checkpoint_repository import save_checkpoints
from database.shipment_repository import save_shipment
from tracking.tracking_service import normalize_aftership_response


def track_live(cn: str) -> dict | None:
    clean_cn = cn.strip().upper()
    print(f"=== Querying AfterShip for Consignment: {clean_cn} ===")

    # Step 1: Check existing trackings on AfterShip account
    tracking_id = find_tracking_id(clean_cn)

    if not tracking_id:
        print("Not yet registered on AfterShip account. Registering new tracking...")
        res = create_tracking(clean_cn)
        if res and isinstance(res, dict):
            data_field = res.get("data", {})
            if isinstance(data_field, dict):
                tracking_id = data_field.get("id") or data_field.get("tracking", {}).get("id")
            if tracking_id:
                print(f"Successfully registered on AfterShip! Tracking ID: {tracking_id}")
            else:
                print(f"Registration response: {res}")
        else:
            print("Failed to register tracking on AfterShip.")

    if not tracking_id:
        print("ERROR: Could not resolve or register tracking on AfterShip.")
        return None

    # Step 2: Retrieve full live tracking details
    print(f"Fetching full tracking details for ID: {tracking_id}...")
    full_resp = get_tracking(tracking_id)
    if not full_resp:
        print("ERROR: Could not retrieve tracking details from AfterShip.")
        return None

    normalized = normalize_aftership_response(full_resp)
    if not normalized:
        print("ERROR: Could not normalize AfterShip tracking payload.")
        return None

    # Step 3: Save to local database
    shipment_id = save_shipment(normalized, provider_tracking_id=tracking_id)
    saved_count = 0
    if normalized.get("checkpoints"):
        saved_checkpoints = save_checkpoints(shipment_id, normalized["checkpoints"])
        saved_count = int(saved_checkpoints)

    normalized["shipment_id"] = shipment_id
    normalized["saved_checkpoints_count"] = saved_count
    return normalized


if __name__ == "__main__":
    cn = "CU488060679IN"
    chat_id = None

    args = sys.argv[1:]
    if args:
        cn = args[0]
        if len(args) > 1:
            chat_id = args[1]
        elif "--chat-id" in args:
            idx = args.index("--chat-id")
            if idx + 1 < len(args):
                chat_id = args[idx + 1]

    # Default to user's known Telegram chat ID if not specified
    if not chat_id:
        chat_id = "9139217221"

    result = track_live(cn)
    if result:
        print("\n==================================================")
        print("  INDIA POST LIVE SHIPMENT STATUS")
        print("==================================================")
        print(f"Consignment Number : {result.get('tracking_number')}")
        print(f"Carrier            : {result.get('carrier')}")
        print(f"Current Status     : {result.get('current_status')}")
        print(f"Status Message     : {result.get('status_message')}")
        print(f"Origin             : {result.get('origin')}")
        print(f"Destination        : {result.get('destination')}")
        print(f"Database Record ID : Shipment #{result.get('shipment_id')}")
        print(f"Total Checkpoints  : {len(result.get('checkpoints', []))}")
        print("--------------------------------------------------")
        for i, cp in enumerate(result.get("checkpoints", []), 1):
            ts = cp.get("date", "N/A")
            loc = cp.get("location") or "In Transit"
            status = cp.get("status", "")
            msg = cp.get("message", "")
            print(f"  {i:02d}. [{ts}] ({loc}) {status} - {msg}")
        print("==================================================")

        if chat_id:
            try:
                from api.telegram_routes import _format_shipment_telegram_card, _send_telegram_reply
                card = _format_shipment_telegram_card(result)
                print(f"\nDispatching live tracking card to Telegram chat: {chat_id}...")
                _send_telegram_reply(chat_id, card)
                print("Notification dispatched to Telegram successfully!")
            except Exception as exc:
                print(f"Warning: Could not dispatch Telegram card: {exc}")


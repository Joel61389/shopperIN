import base64
from datetime import datetime
import requests
from django.conf import settings


def format_kenyan_phone(phone: str) -> str:
    phone = phone.strip().replace("+", "").replace(" ", "")
    if phone.startswith("0"):
        return "254" + phone[1:]
    if phone.startswith("7") or phone.startswith("1"):
        return "254" + phone
    return phone


def get_daraja_base_url() -> str:
    if settings.MPESA_ENV == "production":
        return "https://api.safaricom.co.ke"
    return "https://sandbox.safaricom.co.ke"


def get_access_token() -> str:
    url = f"{get_daraja_base_url()}/oauth/v1/generate?grant_type=client_credentials"
    response = requests.get(
        url,
        auth=(settings.MPESA_CONSUMER_KEY, settings.MPESA_CONSUMER_SECRET),
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def initiate_stk_push(phone_number: str, amount: int, account_ref: str, description: str) -> dict:
    access_token = get_access_token()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    raw_password = f"{settings.MPESA_SHORTCODE}{settings.MPESA_PASSKEY}{timestamp}"
    password = base64.b64encode(raw_password.encode()).decode("utf-8")

    formatted_phone = format_kenyan_phone(phone_number)
    url = f"{get_daraja_base_url()}/mpesa/stkpush/v1/processrequest"
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {
        "BusinessShortCode": settings.MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount),
        "PartyA": formatted_phone,
        "PartyB": settings.MPESA_SHORTCODE,
        "PhoneNumber": formatted_phone,
        "CallBackURL": settings.MPESA_CALLBACK_URL,
        "AccountReference": account_ref[:12],
        "TransactionDesc": description[:13],
    }

    response = requests.post(url, json=payload, headers=headers, timeout=20)
    return response.json()
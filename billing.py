"""YooKassa requests with durable quotes and bounded idempotent recovery."""
import json
import re
from datetime import timedelta
from decimal import Decimal

import httpx
import db

API_URL = 'https://api.yookassa.ru/v3/payments'


class PaymentUncertain(ValueError):
    """No new request may replace a payment whose outcome is unknown."""


class PaymentRejected(ValueError):
    """The provider definitively refused creation, before a payment existed."""


def validate_payment(payment, row):
    if not isinstance(payment,dict):
        raise ValueError('Invalid payment response')
    expected = json.loads(row['metadata_json'])
    meta, amount = payment.get('metadata') or {}, payment.get('amount') or {}
    if (payment.get('id') != row['payment_id'] or not expected.get('project_key')
            or meta.get('project_key') != expected['project_key']
            or str(meta.get('user_id')) != str(row['tg_user_id'])
            or meta.get('kind') != 'channel_access' or meta.get('tariff_code') != row['plan_id']
            or str(meta.get('days')) != str(expected.get('days')) or amount.get('currency') != 'RUB'
            or Decimal(str(amount.get('value'))) != Decimal(str(row['amount_rub']))):
        raise ValueError('Payment identity, amount or quote mismatch')
    if payment.get('status') not in {'pending','waiting_for_capture','succeeded','canceled'}:
        raise ValueError('Unknown payment status')
    if payment['status']=='succeeded' and payment.get('paid') is not True:
        raise ValueError('Payment is not paid')
    return payment


async def submit_checkout(order, shop_id, secret):
    if order['payment_id']:
        return await db.get_payment(order['payment_id'])
    if order['status'] != 'creating':
        raise PaymentRejected('Счёт не создан. Выберите оплату заново.')
    await db.touch_checkout(order['id'])
    if order['cycle_until']:
        contract = await db.get_renewal(order['tg_user_id'])
        if not contract or not contract['enabled'] or await db.get_setting('auto_renew_enabled','0')!='1':
            raise PaymentUncertain('Автопродление отключено. Неизвестный прошлый запрос требует проверки без нового списания.')
    if db.parse_dt(order['created_at']) < db.utcnow()-timedelta(hours=23):
        raise PaymentUncertain('Результат старого запроса проверяется. Новый платёж не создан: обратитесь в поддержку.')
    payload = json.loads(order['request_json'])
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(API_URL,json=payload,auth=(shop_id,secret),
                                         headers={'Idempotence-Key':order['id']})
            response.raise_for_status()
            payment = response.json()
    except httpx.HTTPStatusError as exc:
        # Never infer failure from network errors, 409, 429 or 5xx.
        if exc.response.status_code in {400,401,403,404,422}:
            await db.checkout_failed(order['id'],f'http_{exc.response.status_code}')
            raise PaymentRejected('ЮKassa не создала платёж. Доступ и баланс не менялись.') from exc
        raise
    payment_id = payment.get('id') if isinstance(payment,dict) else None
    if not isinstance(payment_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',payment_id):
        raise ValueError('Missing payment ID')
    row = dict(payment_id=payment_id,tg_user_id=order['tg_user_id'],plan_id=order['plan_id'],
               amount_rub=order['amount_rub'],metadata_json=json.dumps(payload['metadata']))
    validate_payment(payment,row)
    await db.attach_checkout(order,payment)
    return await db.get_payment(payment_id)


async def refresh_payment(row, shop_id, secret):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',row['payment_id']):
        raise ValueError('Invalid payment ID')
    await db.touch_payment(row['payment_id'])
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{API_URL}/{row['payment_id']}",auth=(shop_id,secret))
        response.raise_for_status()
        payment = response.json()
    validate_payment(payment,row)
    await db.update_payment_status(row['payment_id'],payment['status'],payment)
    return payment


async def finalize(row):
    row = await db.get_payment(row['payment_id'])
    if not row:
        raise ValueError('Unknown payment')
    metadata = json.loads(row['metadata_json'])
    validate_payment(json.loads(row['raw_json']),row)
    days = int(metadata['days'])
    return await db.finalize_payment_once(row['payment_id'],row['tg_user_id'],metadata['project_key'],
                                          'channel_access',row['plan_id'],days)

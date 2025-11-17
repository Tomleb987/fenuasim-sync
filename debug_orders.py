import os
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

print("🔌 Connexion à Odoo…")

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

print("🔎 Lecture des 20 dernières commandes…")

order_ids = models.execute_kw(
    ODOO_DB, uid, ODOO_PASSWORD,
    'sale.order', 'search',
    [[]],
    {'limit': 20, 'order': 'id desc'}
)

orders = models.execute_kw(
    ODOO_DB, uid, ODOO_PASSWORD,
    'sale.order', 'read',
    [order_ids],
    {'fields': ['id', 'name', 'origin', 'state', 'order_line']}
)

print(f"📦 {len(orders)} commandes analysées :\n")

for o in orders:
    print(f"ID {o['id']}: name={o['name']} | origin={o.get('origin')} | state={o['state']}")

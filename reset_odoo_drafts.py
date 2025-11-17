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

print("🔎 Recherche des devis Odoo sans origin…")

draft_ids = models.execute_kw(
    ODOO_DB, uid, ODOO_PASSWORD,
    'sale.order', 'search',
    [[
        ('state', '=', 'draft'),
        ('origin', '=', False),
        ('create_date', '>=', '2025-11-15')
    ]]
)

print(f"🗑 {len(draft_ids)} devis trouvés.")

if draft_ids:
    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'unlink',
        [draft_ids]
    )
    print("✅ Devis supprimés.")
else:
    print("ℹ️ Aucun devis à supprimer.")

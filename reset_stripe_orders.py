import os
import xmlrpc.client

# Variables d'environnement
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

if not ODOO_URL or not ODOO_DB or not ODOO_USER or not ODOO_PASSWORD:
    print("❌ Variables d'environnement Odoo manquantes.")
    exit(1)

# Connexion XML-RPC
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

print("🔎 Recherche des commandes Stripe…")

# Rechercher les commandes où origin = "Stripe"
order_ids = models.execute_kw(
    ODOO_DB, uid, ODOO_PASSWORD,
    'sale.order', 'search',
    [[('origin', '=', 'Stripe')]]
)

print(f"🗑 {len(order_ids)} commandes Stripe trouvées.")

# Suppression
if order_ids:
    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'unlink',
        [order_ids]
    )
    print("✅ Commandes Stripe supprimées avec succès.")
else:
    print("ℹ️ Aucune commande Stripe à supprimer.")

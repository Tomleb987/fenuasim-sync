import os
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

print("🔌 Connexion Odoo…")

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

def wipe(model, domain=None):
    if domain is None:
        domain = []
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, 'search', [domain])
    if ids:
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, 'unlink', [ids])
        print(f"🗑 {model} : {len(ids)} supprimés.")
    else:
        print(f"ℹ️ {model} : aucun enregistrement.")

print("🔥 Reset FULL Odoo — suppression de toutes les données…")

# 1. Attachments
wipe('ir.attachment')

# 2. Paiements
wipe('account.payment')

# 3. Factures
wipe('account.move', [('move_type', 'in', ['out_invoice', 'out_refund'])])

# 4. Lignes comptables
wipe('account.move.line')

# 5. Commandes vente
wipe('sale.order')

# 6. Lignes de commandes
wipe('sale.order.line')

# 7. Clients / Contacts (hors entreprises)
wipe('res.partner', [('is_company', '=', False)])

# 8. Produits
wipe('product.product')
wipe('product.template')

# 9. Catégories produits
wipe('product.category')

print("✅ RESET COMPLET TERMINÉ.")

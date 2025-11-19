import os
import requests
from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_API_KEY = os.getenv("ODOO_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def odoo_rpc(service, method, args):
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": service,
            "method": method,
            "args": args
        },
        "id": 1,
    }
    r = requests.post(f"{ODOO_URL}/jsonrpc", json=payload)
    r.raise_for_status()
    return r.json()

def sync_leads():
    # 1️⃣ Récupérer les leads non synchronisés
    leads = supabase.table("leads").select("*").eq("odoo_synced", False).execute().data

    if not leads:
        print("Aucun lead à synchroniser.")
        return

    print(f"{len(leads)} leads à synchroniser…")

    # 2️⃣ Authentification Odoo
    auth = odoo_rpc(
        "common",
        "authenticate",
        [ODOO_DB, ODOO_USER, ODOO_API_KEY, {}]
    )

    uid = auth.get("result")
    if not uid:
        print("ERREUR : impossible de se connecter à Odoo.")
        return

    # 3️⃣ Synchronisation dans Odoo
    for lead in leads:
        payload = {
            "name": f"Lead FENUA SIM – {lead['first_name']} {lead['last_name']}",
            "contact_name": f"{lead['first_name']} {lead['last_name']}",
            "email_from": lead["email"],
            "type": "lead",
            "description": f"Capté via popup FENUA SIM\nSource: {lead.get('source', 'popup')}",
        }

        res = odoo_rpc(
            "object",
            "execute_kw",
            [
                ODOO_DB,
                uid,
                ODOO_API_KEY,
                "crm.lead",
                "create",
                [payload],
            ],
        )

        if "result" in res:
            supabase.table("leads").update({"odoo_synced": True}).eq("id", lead["id"]).execute()
            print(f"Lead synchronisé → Odoo ID {res['result']}")
        else:
            print("Erreur sur ce lead :", res)

if __name__ == "__main__":
    sync_leads()

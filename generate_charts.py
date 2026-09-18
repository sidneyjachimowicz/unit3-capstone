import psycopg2
import matplotlib
matplotlib.use("Agg")  # non-interactive backend, safe for headless CloudShell
import matplotlib.pyplot as plt

REDSHIFT_HOST = "capstone-docsearch-wg.567781376107.us-east-1.redshift-serverless.amazonaws.com"
REDSHIFT_PORT = 5439
REDSHIFT_DB = "dev"
REDSHIFT_USER = "capstoneadmin"
REDSHIFT_PASSWORD = "TrainingAWS123"

conn = psycopg2.connect(
    host=REDSHIFT_HOST, port=REDSHIFT_PORT, dbname=REDSHIFT_DB,
    user=REDSHIFT_USER, password=REDSHIFT_PASSWORD,
)
cur = conn.cursor()

# --- Chart 1: Monthly revenue by customer (bar chart) ---
cur.execute("SELECT customer_name, monthly_revenue FROM public.customer_data ORDER BY monthly_revenue DESC;")
rows = cur.fetchall()
names = [r[0] for r in rows]
revenues = [float(r[1]) for r in rows]

plt.figure(figsize=(8, 5))
plt.bar(names, revenues, color="#4C72B0")
plt.title("Monthly Revenue by Customer")
plt.xlabel("Customer")
plt.ylabel("Monthly Revenue ($)")
plt.tight_layout()
plt.savefig("chart_revenue_by_customer.png", dpi=150)
plt.close()
print("Saved chart_revenue_by_customer.png")

# --- Chart 2: Customer count by plan tier (pie chart) ---
cur.execute("SELECT plan_tier, COUNT(*) FROM public.customer_data GROUP BY plan_tier;")
rows = cur.fetchall()
tiers = [r[0] for r in rows]
counts = [r[1] for r in rows]

plt.figure(figsize=(6, 6))
plt.pie(counts, labels=tiers, autopct="%1.0f%%", colors=["#4C72B0", "#DD8452", "#55A868"])
plt.title("Customer Distribution by Plan Tier")
plt.tight_layout()
plt.savefig("chart_customers_by_tier.png", dpi=150)
plt.close()
print("Saved chart_customers_by_tier.png")

cur.close()
conn.close()
print("Done. Both charts generated from live Redshift data.")

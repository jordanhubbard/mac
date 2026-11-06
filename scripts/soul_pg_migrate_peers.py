import sys, json, subprocess, time, datetime
sys.path.insert(0, '/home/jkh/.mac/src/mac/src')
import psycopg

PG_URL = "postgresql://natasha:natasha2026@100.72.16.110:5432/agent_brain"

def migrate_agent(agent_name, soul_json_path, ssh_target):
    print(f"\n=== {agent_name} from {ssh_target} ===")
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", ssh_target, f"cat {soul_json_path}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}"); return 0

    data = json.loads(result.stdout)
    if isinstance(data, dict) and "nodes" in data:
        nodes = data["nodes"]
    elif isinstance(data, list):
        nodes = {n["id"]: n for n in data}
    else:
        nodes = data

    print(f"  Soul.json nodes: {len(nodes)}")
    conn = psycopg.connect(PG_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM soul_nodes WHERE agent = %s", (agent_name,))
        existing = {row[0] for row in cur.fetchall()}
        print(f"  Already in PG: {len(existing)}")
        inserted = skipped = edges = 0
        for nid, node in nodes.items():
            if nid in existing:
                skipped += 1; continue
            tags = list(node.get("tags", []))
            parents = node.get("parents", [])
            def to_dt(val):
                if val is None:
                    return datetime.datetime.now(tz=datetime.timezone.utc)
                if isinstance(val, (int, float)):
                    return datetime.datetime.fromtimestamp(float(val), tz=datetime.timezone.utc)
                # ISO string e.g. '2026-11-06T18:09:00Z'
                return datetime.datetime.fromisoformat(val.replace("Z", "+00:00"))
            created_dt = to_dt(node.get("created_at"))
            accessed_dt = to_dt(node.get("last_accessed"))
            cur.execute("""
                INSERT INTO soul_nodes
                  (id, content, tags, parents, pinned, score, created_at,
                   updated_at, last_accessed, access_count, metadata, agent)
                VALUES (%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
            """, (nid, node.get("content",""), tags, parents,
                  node.get("pinned", False), 0.0, created_dt, accessed_dt,
                  node.get("access_count", 0), json.dumps(node.get("metadata",{})),
                  agent_name))
            inserted += 1
        for nid, node in nodes.items():
            for pid in node.get("parents", []):
                if pid in nodes:
                    cur.execute("""
                        INSERT INTO soul_edges (parent_id, child_id, relation)
                        VALUES (%s,%s,'child') ON CONFLICT DO NOTHING
                    """, (pid, nid))
                    edges += 1
        conn.commit()
    print(f"  Inserted: {inserted}, skipped: {skipped}, edges: {edges}")

migrate_agent("rocky",      "/Users/jkh/.hermes/soul.json", "jkh@100.72.16.110")
migrate_agent("bullwinkle", "/home/jkh/.hermes/soul.json",  "jkh@100.121.27.109")

print("\n=== Final state ===")
conn = psycopg.connect(PG_URL)
with conn.cursor() as cur:
    cur.execute("SELECT agent, COUNT(*) FROM soul_nodes GROUP BY agent ORDER BY agent")
    for a, c in cur.fetchall(): print(f"  {a}: {c} nodes")
    cur.execute("SELECT COUNT(*) FROM soul_edges")
    print(f"  total edges: {cur.fetchone()[0]}")

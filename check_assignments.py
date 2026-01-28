import requests
import os

CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]
CANVAS_BASE = os.environ["CANVAS_BASE"]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]

headers = {
    "Authorization": f"Bearer {CANVAS_TOKEN}"
}

def get_courses():
    url = f"{CANVAS_BASE}/api/v1/courses?enrollment_state=active"
    return requests.get(url, headers=headers).json()

def get_assignments(course_id):
    url = f"{CANVAS_BASE}/api/v1/courses/{course_id}/assignments?include[]=submission"
    return requests.get(url, headers=headers).json()

def find_bad_assignments():
    bad = []

    for course in get_courses():
        name = course["name"]
        for a in get_assignments(course["id"]):
            sub = a.get("submission")
            if not sub:
                continue

            missing = sub.get("missing", False)
            score = sub.get("score")
            points = a.get("points_possible", 100)

            percent = (score / points * 100) if score is not None else 0

            if missing or percent < 50:
                bad.append(f"**{name}** – {a['name']} ({percent:.1f}%)")

    return bad

def send_to_discord(lines):
    if not lines:
        msg = "🎉 No missing or failing assignments today!"
    else:
        msg = "⚠️ Assignments to fix:\n" + "\n".join(lines)

    requests.post(DISCORD_WEBHOOK, json={"content": msg})

if __name__ == "__main__":
    send_to_discord(find_bad_assignments())

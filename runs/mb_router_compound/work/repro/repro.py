from mailbox_router import MailboxRouter

r = MailboxRouter(capacity_per_topic=3)
for i in range(4):
    r.route("orders", f"m{i}")
print("BUG1: routed 4 messages into a cap=3 topic, no OverflowError")

r2 = MailboxRouter()
r2.route("jobs", "a")
try:
    got = r2.drain("jobs", 5)
    print("drained:", got)
except IndexError as exc:
    print(f"BUG2: drain panicked mid-way: IndexError: {exc}")

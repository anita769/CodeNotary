from dispatcher import Dispatcher

received = []
def flaky(payload):
    if payload == "b":
        raise RuntimeError("consumer down")
    received.append(payload)

d = Dispatcher()
d.subscribe("orders", flaky)
for p in ["a", "b", "c"]:
    d.publish("orders", p)
try:
    d.dispatch_all()
except RuntimeError as exc:
    print(f"dispatch crashed: {exc}")
print("received:", received)
print("pending:", d.pending())
print("BUG1: 'b' was popped before delivery and is now lost (no retry, no dead letter)")

d2 = Dispatcher()
d2.publish("ghost", "x")
d2.dispatch_all()
print("BUG2: unknown-channel message silently dropped; dead_letters:", getattr(d2, "dead_letters", "<no such audit list>"))

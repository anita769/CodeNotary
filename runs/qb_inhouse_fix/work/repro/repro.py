from queue_box import Mailbox
box = Mailbox()
try:
    box.pop()
except IndexError as exc:
    print(f'IndexError: {exc}')

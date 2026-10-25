# Delivery

A successful exit can still mean nothing was delivered; read what each route
reports instead of assuming the human received the notice. When one route
succeeds and another fails, do not resend the notice; record what was omitted.
When nothing was delivered, do not retry in a loop or try to configure delivery
yourself. Fall back in this order:

1. The harness's own notification tool, when it has one.
2. The record: write the notice where the human looks first (the work item's
   decisions, or your report), and put it at the top of your next message.

Then keep working on whatever doesn't wait on their answer, and say in your
report that delivery was inactive or failed and which warnings or errors you
saw. Leave credentials and tokens out of messages and reports.


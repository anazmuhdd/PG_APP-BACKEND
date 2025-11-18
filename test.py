import datetime

order_date = "2025-11-19"

now = datetime.datetime.utcnow()  # IST timezone adjustment
now=now + datetime.timedelta(hours=5, minutes=30)  # adjust minutes if needed

order_date_obj = datetime.datetime.strptime(order_date, "%Y-%m-%d").date()

print("order_date (string):", order_date)
print("order_date (date):", order_date_obj)
print("today_date:", now.date())
print("current datetime:", now)
print("current time:", now.time())

intent=""
# example checks
if order_date_obj == now.date()+datetime.timedelta(days=1):
    intent="tomorrow"
    print("order_date is tomorrow")
    
elif order_date_obj == now.date():
    intent="today"
else:
    print("order_date is futures")
    
breakfast_lunch_cutoff = datetime.time(21, 30)  # 9:30 PM previous day
dinner_cutoff = datetime.time(12, 30)  # 12:30 PM same day

breakfast=1
lunch=1
dinner=1

if(breakfast==1 or lunch==1):
    if (now.time() > breakfast_lunch_cutoff and intent=="tomorrow"):
        print("Cannot order breakfast or lunch for", intent, "after cutoff time")
    else:
        print("Can order breakfast or lunch for", intent)
if(dinner==1):
    if now.time() > dinner_cutoff:
        print("Cannot order dinner for", intent, "after cutoff time")
    else:
        print("Can order dinner for", intent)
print("breakfast_lunch_cutoff:", breakfast_lunch_cutoff)
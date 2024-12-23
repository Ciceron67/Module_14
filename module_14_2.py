import sqlite3

connection = sqlite3.connect('not_telegram.db')
cursor = connection.cursor()

cursor.execute('''CREATE TABLE IF NOT EXISTS Users(
id INTEGER PRIMARY KEY,
username TEXT NOT NULL,
email TEXT NOT NULL,
age INTEGER,
balance INTEGER NOT NULL 
)''')

# cursor.execute('delete from Users where id = 6')
cursor.execute('select count (*) from Users')
total_users = cursor.fetchone()[0]
cursor.execute('select sum (balance) from Users')
all_balances = cursor.fetchone()[0]
print(all_balances/total_users)

connection.commit()
connection.close()


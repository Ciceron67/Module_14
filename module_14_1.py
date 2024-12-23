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



# for i in range(1,11):
#     cursor.execute('INSERT INTO Users (username, email, age, balance) VALUES(?, ?, ?, ?)',
#                    (f'User{i}', f'example{i}@gmail.com', f'{i*10}', '1000'))
#
# for i in range(1, 11, 2):
#     cursor.execute(f'update Users set balance = 500 where id = {i}')
#
# for i in range(1, 11, 3):
#     cursor.execute(f'delete from Users where id = {i}')

cursor.execute('select * from Users' )
all_users = cursor.fetchall()
for i in all_users:
    print(f'Имя:{i[1]}|Почта:{i[2]}|Возраст:{i[3]}|Баланс:{i[4]}')

connection.commit()
connection.close()


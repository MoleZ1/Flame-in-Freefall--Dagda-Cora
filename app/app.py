from flask import Flask, request, render_tempalte, redirect, url_for
from pymongo import MongoClient


app=Flask(__name__)

MONGO_URI = 'mongodb://localhost:27017/'
DB_NAME = 'psi_science'

@app.route('/')
def principal():
    return render_template('index.html')

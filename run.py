"""Start the prototype:  python run.py   then open http://127.0.0.1:5000"""
from tms import create_app

if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)

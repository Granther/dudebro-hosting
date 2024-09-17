import os
from io import BytesIO
from multiprocessing import Process, Queue
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import shutil

from flask import Flask, render_template, redirect, url_for, flash, request, jsonify, send_file, copy_current_request_context
from flask_bcrypt import Bcrypt
from flask_socketio import SocketIO, join_room
from flask_cors import CORS
from flask_login import LoginManager, login_user, current_user, logout_user, login_required

from db_factory import db
from models import Users, Containers
from deploy import Deploy
from properties import Properties
from logger import create_logger
from rcon import DudeRcon
from forms import RegistrationForm, LoginForm, CommandForm, DeleteForm, \
                    ServerCreateForm, ServerPropertiesForm, CommandSelectForm

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "backup-key")
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URI", "sqlite:///dudebro.db")

db.init_app(app)
bcrypt = Bcrypt(app)
CORS(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = None
login_manager.login_message_category = 'info'

with app.app_context():
    db.create_all()

properties = Properties()
deploy = Deploy(image=os.getenv("IMAGE_NAME"), db=db, Containers=Containers)
rcon = DudeRcon()
executor = ThreadPoolExecutor(max_workers=2)
socketio = SocketIO(app, cors_allowed_origins="*")

logger = create_logger(__name__)
vanity = {"start": "Running", "stop": "Stopping", "die": "Off", "restart": "Running"}

states = [{"status": "start", "show": "Running", "color": "bg-green-500"},
            {"status": "die", "show": "Stopped", "color": "bg-red-500"},
            {"status": "restart", "show": "Running", "color": "bg-green-500"},
            {"status": "stop", "show": "Stopping", "color": "bg-orange-500"}]

@login_manager.user_loader
def load_user(user_id):
    return Users.query.get(int(user_id))

@socketio.on('connect')
def handle_connect():
    print("Client connected")
    room = current_user.id
    join_room(room)

    socketio.emit('message', 'connected to room', room=room)

@app.route("/monitor")
def monitor():
    @copy_current_request_context
    def monitor_events(uid):
        for event in deploy.client.events(decode=True):
            if event.get('Type') == 'container':
                action = event.get('Action')
                if action in ['start', 'stop', 'die', 'restart']:
                    for state in states:
                        if state['status'] == action:
                            socketio.emit('container_status', state, room=uid)
    
    socketio.start_background_task(monitor_events, current_user.id)
    return jsonify({"status": True})

def user_can_access(id: int, subdomain: str) -> bool:
    results = Users.query.filter_by(id=id).first().containers

    if results:
        for container in results:
            if subdomain == container.subdomain:
                return True
        
    return False

def reached_creation_limit(id: int):
    results = Users.query.filter_by(id=id).first()

    if results:  
        if len(results.containers) >= results.container_limit:
            return True
    
    return False

def get_container(subdomain: str):
    containers = deploy.get_user_containers(subdomain=subdomain)
    if containers:
        return containers[0]
    
    return False

def get_container_status(subdomain: str):
    states = [{"status": "running", "show": "Running", "color": "bg-green-500"},
                {"status": "exited", "show": "Stopped", "color": "bg-red-500"},
                {"status": "restarting", "show": "Restarting", "color": "bg-orange-500"}]

    container = get_container(subdomain)
    if not container:
        return False

    status = deploy.get_status(container.id)

    for state in states:
        if state['status'] == status:
            return state

    return False

def send_rcon_command(subdomain: str, command: str):
    container_ip = deploy.get_container_ip(deploy._get_container(subdomain=subdomain))
    rcon_port = Containers.query.filter_by(subdomain=subdomain).first().rcon_port

    queue= Queue()

    # Run in a new process, thus, it will run on the main thread. Sidestepping ValueError raised by `signals`
    p = Process(target=rcon.command, args=(queue, command, rcon_port, container_ip))
    p.start()
    p.join()
    response = queue.get()

    logger.info(f"Sent command: {command}, got response: {response}")

    return response

def authorized(f):
    """Decorator for handling if a user is authorized in regards to login and container access"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('login'))
           
        subdomain = kwargs.get("subdomain", False)

        if not subdomain:
            return f(*args, **kwargs)

        if not user_can_access(current_user.id, subdomain):
            flash("Sorry, you cannot access that page", "danger")
            return redirect(url_for('home'))
        else:
            # Return original view function
            return f(*args, **kwargs)

    return decorated_function
   
@app.route("/register", methods=['GET', 'POST'])
def register():
    form = RegistrationForm()
    if form.validate_on_submit():
        hashed_password = bcrypt.generate_password_hash(form.password.data).decode('utf-8')
        email = Users.query.filter_by(email=form.email.data).first()
        if email:
            flash('Register Unsuccessful. Email already associated with account', 'danger')
            return render_template("register.html", title='Register', form=form)

        user = Users(username=form.username.data, email=form.email.data, password=hashed_password)
        db.session.add(user)
        db.session.commit()
        flash('Your account has been created! You can now log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html', title='Register', form=form)

@app.route("/login", methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = Users.query.filter_by(email=form.email.data).first()
        if user and bcrypt.check_password_hash(user.password, form.password.data):
            login_user(user, remember=form.remember.data)
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('home'))
        else:
            flash('Login Unsuccessful. Please check email and password', 'danger')

    return render_template('login.html', title='Login', form=form)

@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/about')
def about():
    return render_template('about.html')

@app.route("/home", methods=['GET', 'POST'])
@login_required
def home():
    form = ServerCreateForm()
    servers = []
    results = Users.query.filter_by(email=current_user.email).first()

    if results:
        containers = results.containers
        for item in containers:
            x = deploy.get_status(str(item.id))
            print(x)
            servers.append(item)

    if form.validate_on_submit():
        if reached_creation_limit(id=current_user.id):
            flash("Sorry, you have reached the maximum number of servers you can create", "danger")
            return redirect(url_for('home'))  

        try:
            deploy.create_container(user_id=current_user.id, subdomain=form.subdomain.data)
            flash("Successfully created server", "success")
            return redirect(url_for('home'))
        except Exception as e:
            flash(f"Exception occured when creating server: {e}","danger")
            return redirect(url_for('home'))
        
        # return render_template("home.html", servers=servers, form=form)
    return render_template("home.html", servers=servers, form=form, domain=os.getenv("DOMAIN"))

@app.route("/home/<subdomain>")
@authorized
def server(subdomain):
    return render_template("server.html", subdomain=subdomain)
    
@app.route("/edit/<subdomain>", methods=['POST', 'GET'])
@authorized
def edit(subdomain):
    form = ServerPropertiesForm()
    command_form = CommandForm()
    delete_form = DeleteForm()
    command_select_form = CommandSelectForm()
    
    results = Containers.query.filter_by(subdomain=subdomain).first()
    props = properties.read_server_properties(results.uuid)

    if request.method == 'GET':
        for key, val in props.items():
            try:
                key = key.replace("-", "_")
                getattr(form, key).data = val
            except:
                pass

    if form.validate_on_submit():
        for key, val in props.items():
            try:
                props[key] = getattr(form, key.replace("-", "_")).data
            except:
                pass

        properties.write_server_properties(results.uuid, props)
        executor.submit(async_restart, subdomain)

    return render_template("edit.html", form=form, command_form=command_form, 
                           delete_form=delete_form, subdomain=subdomain, command_select_form=command_select_form, domain=os.getenv("DOMAIN"))

@app.route("/command/<subdomain>", methods=['POST'])
@authorized
def command(subdomain):
    form = CommandSelectForm()

    if form.validate_on_submit():
        res = send_rcon_command(subdomain, f"{form.command.data} {form.input.data}")

        logger.debug(form.command.data)

        return jsonify({"response": res})
    
    return jsonify({"status": "nope"})

@app.route("/delete/<subdomain>", methods=['POST'])
@authorized
def delete(subdomain):
    # Maybe not actually delete

    form = ServerPropertiesForm()
    command_form = CommandForm()
    delete_form = DeleteForm()

    if delete_form.validate_on_submit():
        logger.info(f"Deleting container attached to subdomain: {subdomain}")
        deploy.delete_container(subdomain)

        return redirect(url_for('home'))

    return render_template("edit.html", form=form, command_form=command_form, 
                           delete_form=delete_form, subdomain=subdomain, domain=os.getenv("DOMAIN"))


@app.route("/get_status/<subdomain>", methods=['GET', 'POST'])
@authorized
def get_status(subdomain):
    if request.method == "GET":
        status = get_container_status(subdomain)

        print(status)

        if status:
            return jsonify(status)
        else:
            return jsonify({"status": "unknown", "show": "Unknown", "color": "bg-gray-500"})
    
    return "Unauthorized", 401

def async_restart(subdomain):
    container = get_container(subdomain=subdomain)  # Fetch the container by subdomain
    container.restart()  # Restart the container
    logger.debug(f"Container {subdomain} restarted.")
    return f"Container {subdomain} restarted."

@app.route("/restart/<subdomain>", methods=['GET', 'POST'])
@authorized
def restart(subdomain):
    if request.method == "GET":
        status = True

        # get_container(subdomain=subdomain).restart()
        executor.submit(async_restart, subdomain)

        return jsonify({"status": "restarting", "show": "Restarting", "color": "bg-orange-500"})
    
    return "Unauthorized", 401

@app.route("/shutdown/<subdomain>", methods=['GET', 'POST'])
@authorized
def shutdown(subdomain):
    if request.method == "GET":
        status = True

        get_container(subdomain=subdomain).kill()

        return jsonify(status)
    
    return "Unauthorized", 401

@app.route("/start/<subdomain>", methods=['GET', 'POST'])
@authorized
def start(subdomain):
    if request.method == "GET":
        status = True

        get_container(subdomain=subdomain).start()

        return jsonify(status)
    
    return "Unauthorized", 401

def get_world_path(subdomain: str):
    container = Containers.query.filter_by(subdomain=subdomain).first()
    uuid = container.uuid
    instances_dir = os.getenv("INSTANCES_DIR")
    path = f"{instances_dir}/{uuid}"

    shutil.make_archive(path+"/world", 'zip', path+"/world")

    # with ZipFile(path + '/world.zip', 'w') as myzip:
    #     myzip.write(path + "/world")

    return f"{instances_dir}/{uuid}/world.zip"

@app.route("/download/<subdomain>", methods=['GET', 'POST'])
@authorized
def download(subdomain):
    path = get_world_path(subdomain)

    return send_file(path, as_attachment=True)

@app.route("/")
def index():
    return render_template("index.html")

if __name__ == '__main__':
    # app.run(debug=True, host="0.0.0.0", port=5005)
    socketio.run(app, debug=True, host="0.0.0.0", port=1100)

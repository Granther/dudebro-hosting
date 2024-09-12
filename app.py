import logging
import os

from flask import Flask, render_template, redirect, url_for, flash, request
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, current_user, logout_user, login_required
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, BooleanField, SelectField, IntegerField
from wtforms.validators import DataRequired, Length, Email, EqualTo, ValidationError

from db_factory import db
from models import Users, Containers
from deploy import Deploy
from properties import Properties

app = Flask(__name__)
app.config['SECRET_KEY'] = 'glorp'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///dudebro.db'

db.init_app(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

with app.app_context():
    db.create_all()

deploy = Deploy(image="debian", db=db, Containers=Containers)
properties = Properties("server.properties")

logger = logging.getLogger(__name__)
log_level = os.getenv("LOG_LEVEL")
logger.setLevel(log_level)

console_handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

file_handler = logging.FileHandler(os.path.join(os.getenv("LOGS_DIR"), f"{log_level}.log"))
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

@login_manager.user_loader
def load_user(user_id):
    return Users.query.get(int(user_id))

class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=2, max=20)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField('Sign Up')

class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember = BooleanField('Remember Me')
    submit = SubmitField('Login')

class ServerCreateForm(FlaskForm):
    subdomain = StringField('Subdomain', validators=[DataRequired()])
    name = StringField('Name', validators=[DataRequired()])
    submit = SubmitField('Create')

class ServerPropertiesForm(FlaskForm):
    allow_flight = SelectField("allow_flight", choices=[("false", "false"), ("true", "true")])
    allow_nether = SelectField("allow_nether", choices=[("false", "false"), ("true", "true")])
    difficulty = SelectField("difficulty", choices=[("hard", "hard"), ("easy", "easy"), ("peaceful", "peaceful")])
    enforce_whitelist = SelectField("enforce_whitelist", choices=[("false", "false"), ("true", "true")])
    gamemode = SelectField("gamemode", choices=[("creative", "creative"), ("survival", "survival")])
    hardcore = SelectField("hardcore", choices=[("false", "false"), ("true", "true")])
    level_name = StringField("level_name")
    level_seed = StringField("level_seed")
    level_type = StringField("level_type")
    max_players = IntegerField("max_players")
    motd = StringField("motd")
    pvp = SelectField("pvp", choices=[("false", "false"), ("true", "true")])
    # query55port = StringField("query.port")
    # rcon55password = StringField("rcon.password")
    # rcon55port = StringField("rcon.port")
    # server_port = StringField("server_port")
    simulation_distance = IntegerField("simulation_distance")
    view_distance = IntegerField("view_distance")
    white_list = SelectField("white_list", choices=[("false", "false"), ("true", "true")])

    submit = SubmitField('Save')

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
    return redirect(url_for('home'))

@app.route('/about')
def about():
    return render_template('about.html')

@app.route("/home")
@login_required
def home():
    servers = []
    results = Users.query.filter_by(email=current_user.email).first()

    if results:
        containers = results.containers
        for item in containers:
            servers.append(item)

    logger.info(servers)

    return render_template("home.html", servers=servers)

@app.route("/home/<subdomain>")
@login_required
def server(subdomain):
    return render_template("server.html", subdomain=subdomain)

@app.route("/edit/<subdomain>", methods=['POST', 'GET'])
@login_required
def edit(subdomain):
    form = ServerPropertiesForm()
    props = properties.read_server_properties()

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

        properties.write_server_properties(props)
        return redirect(url_for('home'))

    return render_template("edit.html", form=form)

@app.route("/create", methods=['GET', 'POST'])
@login_required
def create():
    form = ServerCreateForm()
    if form.validate_on_submit():
        try:
            deploy.create_container(user_id=current_user.id, subdomain=form.subdomain.data)
            flash("Successfully created server", "success")
            return redirect(url_for('home'))
        except Exception as e:
            flash(f"Exception occured when creating server: {e}","danger")
            return redirect(url_for('home'))
    
    return render_template("create.html", title="Create", form=form)

@app.route("/")
def index():
    return render_template("index.html")

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=5005)

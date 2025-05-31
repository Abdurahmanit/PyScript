from flask import Flask, jsonify, render_template, request, redirect, url_for, session, flash
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import requests
import time
import random
import os
import threading
from datetime import datetime, timedelta
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CollectorRegistry
from prometheus_client.exposition import CONTENT_TYPE_LATEST

# --- Application Setup ---
app = Flask(__name__) # Flask will look for a 'templates' folder in the same directory
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'secret_key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///status_app.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

CORS(app)
db = SQLAlchemy(app)

# --- Prometheus Metrics ---
prometheus_registry = CollectorRegistry()

APP_REQUEST_COUNT = Counter(
    'enhanced_status_app_requests_total',
    'Total requests to EnhancedStatusAggregator API/endpoints',
    ['method', 'endpoint', 'http_status'],
    registry=prometheus_registry
)
APP_REQUEST_LATENCY = Histogram(
    'enhanced_status_app_request_latency_seconds',
    'Request latency for EnhancedStatusAggregator API/endpoints',
    ['endpoint'],
    registry=prometheus_registry
)
USER_SERVICE_POLL_COUNT = Counter(
    'enhanced_status_app_user_service_poll_total',
    'Total polls to user-defined mock services',
    ['user_id', 'service_name', 'status_code_simulated'],
    registry=prometheus_registry
)
USER_SERVICE_POLL_LATENCY = Histogram(
    'enhanced_status_app_user_service_poll_latency_seconds',
    'Latency of polling user-defined mock services',
    ['user_id', 'service_name'],
    registry=prometheus_registry
)
ACTIVE_USERS_GAUGE = Gauge(
    'enhanced_status_app_active_users_total',
    'Total number of currently active (logged-in) users - conceptual',
    registry=prometheus_registry
)
USER_SERVICES_MONITORED_GAUGE = Gauge(
    'enhanced_status_app_user_services_monitored_total',
    'Total number of services being monitored by users',
    registry=prometheus_registry
)
BACKGROUND_POLLER_ERRORS = Counter(
    'enhanced_status_app_background_poller_errors_total',
    'Errors encountered by the background service poller',
    registry=prometheus_registry
)

# --- Database Models ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    services = db.relationship('MonitoredService', backref='owner', lazy=True, cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class MonitoredService(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    mock_url = db.Column(db.String(255), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    current_status = db.Column(db.String(50), default='Unknown')
    last_checked = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'name', name='_user_service_name_uc'),)


# --- Helper Functions ---
def simulate_poll_service(service_id, service_name, user_id_for_metrics):
    start_time = time.time()
    simulated_status_code = "unknown"
    try:
        time.sleep(random.uniform(0.1, 0.5))
        if random.random() < 0.05:
            simulated_status_code = "timeout_error"
            raise requests.exceptions.Timeout("Simulated timeout")
        elif random.random() < 0.10:
            simulated_status_code = "500_simulated_error"
            raise requests.exceptions.HTTPError("Simulated 500 Server Error")
        possible_statuses = ["Operational", "Degraded", "Minor Outage"]
        status = random.choices(possible_statuses, weights=[0.92, 0.05, 0.03], k=1)[0]
        simulated_status_code = "200_simulated_ok"
        final_status = status
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Simulated polling error for service '{service_name}' (ID: {service_id}): {e}")
        final_status = "Major Outage"
    finally:
        latency = time.time() - start_time
        USER_SERVICE_POLL_LATENCY.labels(user_id=str(user_id_for_metrics), service_name=service_name).observe(latency)
        USER_SERVICE_POLL_COUNT.labels(user_id=str(user_id_for_metrics), service_name=service_name, status_code_simulated=simulated_status_code).inc()
    with app.app_context():
        service_to_update = MonitoredService.query.get(service_id)
        if service_to_update:
            service_to_update.current_status = final_status
            service_to_update.last_checked = datetime.utcnow()
            db.session.commit()
        else:
            app.logger.warning(f"Background poller: Service ID {service_id} not found for update.")
    return final_status

# --- Background Polling Thread ---
POLL_INTERVAL_SECONDS = 60
poller_thread_stop_event = threading.Event()

def background_service_poller():
    app.logger.info("Background service poller thread started.")
    while not poller_thread_stop_event.is_set():
        try:
            with app.app_context():
                services_to_poll = MonitoredService.query.all()
                USER_SERVICES_MONITORED_GAUGE.set(len(services_to_poll))
                if not services_to_poll: app.logger.debug("No services to poll.")
                else: app.logger.info(f"Polling {len(services_to_poll)} services...")
                for service in services_to_poll:
                    if poller_thread_stop_event.is_set(): break
                    app.logger.debug(f"Background polling service: {service.name} (ID: {service.id}) for user {service.user_id}")
                    simulate_poll_service(service.id, service.name, service.user_id)
                    poller_thread_stop_event.wait(0.1)
        except Exception as e:
            BACKGROUND_POLLER_ERRORS.inc()
            app.logger.error(f"Error in background poller: {e}", exc_info=True)
            poller_thread_stop_event.wait(POLL_INTERVAL_SECONDS / 2)
        app.logger.debug(f"Background poller finished a cycle. Waiting for {POLL_INTERVAL_SECONDS} seconds.")
        poller_thread_stop_event.wait(POLL_INTERVAL_SECONDS)
    app.logger.info("Background service poller thread stopped.")

# --- Middleware for Metrics ---
@app.before_request
def before_request_metrics(): request.start_time = time.time()

@app.after_request
def after_request_metrics(response):
    if hasattr(request, 'start_time') and request.endpoint:
        latency = time.time() - request.start_time
        APP_REQUEST_LATENCY.labels(endpoint=request.endpoint).observe(latency)
        APP_REQUEST_COUNT.labels(method=request.method, endpoint=request.endpoint, http_status=response.status_code).inc()
    return response

# --- Routes (Auth, Dashboard, Services) ---
@app.route('/')
def index_route():
    if 'user_id' in session: return redirect(url_for('dashboard_route'))
    return render_template('index.html') # Will look for templates/index.html

@app.route('/register', methods=['GET', 'POST'])
def register_route():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if not username or not password:
            flash('Username and password are required.', 'danger')
            return redirect(url_for('register_route'))
        if User.query.filter_by(username=username).first():
            flash('Username already exists.', 'warning')
            return redirect(url_for('register_route'))
        new_user = User(username=username)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('login_route'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login_route():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            session['user_id'] = user.id
            session['username'] = user.username
            session.permanent = True
            flash('Logged in successfully!', 'success')
            return redirect(url_for('dashboard_route'))
        else:
            flash('Invalid username or password.', 'danger')
            return redirect(url_for('login_route'))
    return render_template('login.html')

@app.route('/logout')
def logout_route():
    session.pop('user_id', None)
    session.pop('username', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('index_route'))

@app.route('/dashboard')
def dashboard_route():
    if 'user_id' not in session:
        flash('Please log in.', 'warning')
        return redirect(url_for('login_route'))
    user_id = session['user_id']
    user_services = MonitoredService.query.filter_by(user_id=user_id).order_by(MonitoredService.name).all()
    return render_template('dashboard.html', services=user_services)

@app.route('/services/add', methods=['GET', 'POST'])
def add_service_route():
    if 'user_id' not in session: return redirect(url_for('login_route'))
    if request.method == 'POST':
        name = request.form.get('name')
        mock_url = request.form.get('mock_url')
        if not name or not mock_url:
            flash('Service name and URL are required.', 'danger')
            # Pass current values back to the form for better UX
            return render_template('add_service.html', name=name, mock_url=mock_url)
        user_id = session['user_id']
        existing_service = MonitoredService.query.filter_by(user_id=user_id, name=name).first()
        if existing_service:
            flash(f"A service named '{name}' already exists.", 'warning')
            return render_template('add_service.html', name=name, mock_url=mock_url)
        new_service = MonitoredService(name=name, mock_url=mock_url, user_id=user_id, current_status="Pending First Check")
        db.session.add(new_service)
        db.session.commit()
        flash('Service added!', 'success')
        return redirect(url_for('dashboard_route'))
    return render_template('add_service.html')

@app.route('/services/delete/<int:service_id>', methods=['POST'])
def delete_service_route(service_id):
    if 'user_id' not in session:
        flash('Authentication required.', 'danger')
        return redirect(url_for('login_route'))
    service_to_delete = MonitoredService.query.get_or_404(service_id)
    if service_to_delete.user_id != session['user_id']:
        flash('Not authorized.', 'danger')
        return redirect(url_for('dashboard_route'))
    db.session.delete(service_to_delete)
    db.session.commit()
    flash('Service deleted.', 'success')
    return redirect(url_for('dashboard_route'))

# --- API Endpoints & SRE Routes ---
@app.route('/api/user/services', methods=['GET'])
def api_get_user_services():
    if 'user_id' not in session: return jsonify({"error": "Authentication required"}), 401
    user_id = session['user_id']
    services = MonitoredService.query.filter_by(user_id=user_id).all()
    output = [{"id": s.id, "name": s.name, "mock_url": s.mock_url, "current_status": s.current_status, "last_checked": s.last_checked.isoformat() if s.last_checked else None} for s in services]
    return jsonify({"services": output})

@app.route('/health')
def health_check_route():
    return jsonify({"status": "healthy", "timestamp": datetime.utcnow().isoformat()}), 200

@app.route('/metrics')
def metrics_route():
    return generate_latest(prometheus_registry), 200, {'Content-Type': CONTENT_TYPE_LATEST}

# Add a context processor to make 'now' available to all templates for the copyright year.
@app.context_processor
def inject_now():
    return {'now': datetime.utcnow()}

# --- Main Execution ---
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        app.logger.info("Database tables created/ensured.")

    # Start background poller thread
    # This logic helps prevent the thread from starting multiple times when Flask's reloader is active.
    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        if not (app.debug and os.environ.get('WERKZEUG_RUN_MAIN') == 'true' and hasattr(app, '_poller_started') and app._poller_started):
            poller = threading.Thread(target=background_service_poller, daemon=True)
            poller.start()
            app.logger.info("Background poller thread initiated.")
            if app.debug: # Mark that poller has been started in the main process
                app._poller_started = True
    elif os.environ.get('WERKZEUG_RUN_MAIN') != 'true': # Werkzeug reloader child process
        app.logger.info("Skipping background poller start in Werkzeug reloader child process.")


    app.run(debug=True, host='0.0.0.0', port=5000) # use_reloader=True is default in debug

    # Graceful shutdown attempt for the poller thread
    try:
        pass # app.run blocks here
    except KeyboardInterrupt:
        app.logger.info("Shutdown signal received, stopping poller...")
    finally:
        poller_thread_stop_event.set()
        # Check if 'poller' was defined in the scope (it might not be if WERKZEUG_RUN_MAIN was false)
        if 'poller' in locals() and poller.is_alive():
            poller.join(timeout=5)
        app.logger.info("Application shutdown attempt complete.")
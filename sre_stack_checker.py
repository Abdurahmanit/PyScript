#!/usr/bin/env python3
import requests
import json
import argparse
from urllib.parse import urlparse

# --- Configuration ---
DEFAULT_APP_URL = "http://localhost:5000"
DEFAULT_PROMETHEUS_URL = "http://localhost:9090"
DEFAULT_ALERTMANAGER_URL = "http://localhost:9093"
DEFAULT_APP_JOB_NAME_IN_PROMETHEUS = "enhanced_status_aggregator"
DEFAULT_TIMEOUT_SECONDS = 3

# ANSI escape codes for colors
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    ENDC = '\033[0m'

def print_status(message, success):
    """Prints a message with color-coded status."""
    if success:
        print(f"{Colors.GREEN}[PASS]{Colors.ENDC} {message}")
    else:
        print(f"{Colors.RED}[FAIL]{Colors.ENDC} {message}")

def check_endpoint(name, url, expected_status_code=200, timeout=DEFAULT_TIMEOUT_SECONDS):
    """
    Checks if an HTTP endpoint is responsive and returns the expected status code.
    Returns True if healthy, False otherwise.
    """
    print(f"{Colors.BLUE}Checking: {name} at {url}...{Colors.ENDC}")
    try:
        response = requests.get(url, timeout=timeout)
        if response.status_code == expected_status_code:
            print_status(f"{name} is healthy (Status: {response.status_code})", True)
            return True
        else:
            print_status(f"{name} returned status {response.status_code}. Expected {expected_status_code}.", False)
            return False
    except requests.exceptions.Timeout:
        print_status(f"{name} request timed out after {timeout} seconds.", False)
        return False
    except requests.exceptions.ConnectionError:
        print_status(f"{name} connection refused or DNS failure.", False)
        return False
    except requests.exceptions.RequestException as e:
        print_status(f"{name} is unreachable or encountered an error: {e}", False)
        return False

def check_prometheus_app_target(prometheus_url, app_job_name, app_url, timeout=DEFAULT_TIMEOUT_SECONDS):
    """
    Checks if Prometheus is successfully scraping the application target.
    Returns True if the target is found and 'up', False otherwise.
    """
    targets_api_url = f"{prometheus_url}/api/v1/targets"
    print(f"{Colors.BLUE}Checking: Prometheus target '{app_job_name}' at {targets_api_url}...{Colors.ENDC}")
    try:
        response = requests.get(targets_api_url, timeout=timeout)
        response.raise_for_status() # Raise an exception for HTTP error codes
        targets_data = response.json()

        app_target_found_and_up = False
        app_target_found_but_down = False

        if targets_data.get("status") == "success":
            active_targets = targets_data.get("data", {}).get("activeTargets", [])
            for target_group in active_targets:
                if target_group.get("scrapePool") == app_job_name:
                    # Check if the scrape URL matches the expected app URL (ignoring scheme for flexibility if Prometheus adds it)
                    # More robustly, you'd check discoveredLabels for __address__ or scrapeUrl
                    discovered_labels = target_group.get("discoveredLabels", {})
                    scrape_url_from_target = target_group.get("scrapeUrl", "") # Get the actual scrape URL

                    # A simple check if the app_url is contained within the scrape_url
                    # This handles cases where Prometheus might add http:// or other params
                    parsed_app_url_path = urlparse(app_url).path or "/" # Get path, default to /
                    parsed_scrape_url_path = urlparse(scrape_url_from_target).path or "/"

                    # Check if the job is up and the scrape URL roughly matches
                    # A more precise match might be needed for complex setups
                    if target_group.get("health") == "up":
                         # Check if the scrape URL contains the app's host and port, and the metrics path
                         if urlparse(app_url).netloc in urlparse(scrape_url_from_target).netloc and \
                            (parsed_app_url_path + "/metrics" == parsed_scrape_url_path or # if app_url is base
                             app_url + "/metrics" == scrape_url_from_target): # if app_url includes path
                            app_target_found_and_up = True
                            break
                    else: # Target found but not up
                        app_target_found_but_down = True
                        # Don't break, maybe another instance of the same job is up

            if app_target_found_and_up:
                print_status(f"Prometheus: Target job '{app_job_name}' (scraping {scrape_url_from_target}) is UP.", True)
                return True
            elif app_target_found_but_down:
                print_status(f"Prometheus: Target job '{app_job_name}' was found but is DOWN.", False)
                return False
            else:
                print_status(f"Prometheus: Target job '{app_job_name}' configured to scrape '{app_url}/metrics' not found or not active/up.", False)
                return False
        else:
            print_status(f"Prometheus: API request to {targets_api_url} was not successful (status: {targets_data.get('status')}).", False)
            return False

    except requests.exceptions.RequestException as e:
        print_status(f"Prometheus: Error querying targets API at {targets_api_url}: {e}", False)
        return False
    except json.JSONDecodeError:
        print_status(f"Prometheus: Could not decode JSON response from {targets_api_url}.", False)
        return False


def check_prometheus_alertmanager_link(prometheus_url, alertmanager_url, timeout=DEFAULT_TIMEOUT_SECONDS):
    """
    Checks if Prometheus has discovered and is connected to the configured Alertmanager.
    Returns True if an active Alertmanager matching the URL is found, False otherwise.
    """
    alertmanagers_api_url = f"{prometheus_url}/api/v1/alertmanagers"
    print(f"{Colors.BLUE}Checking: Prometheus to Alertmanager link (expecting {alertmanager_url}) at {alertmanagers_api_url}...{Colors.ENDC}")
    try:
        response = requests.get(alertmanagers_api_url, timeout=timeout)
        response.raise_for_status()
        alertmanagers_data = response.json()

        if alertmanagers_data.get("status") == "success":
            active_alertmanagers = alertmanagers_data.get("data", {}).get("activeAlertmanagers", [])
            if not active_alertmanagers:
                print_status("Prometheus: No active Alertmanagers are configured or discovered.", False)
                return False

            parsed_expected_am_url = urlparse(alertmanager_url)

            for am in active_alertmanagers:
                am_url_from_prometheus = am.get("url")
                if am_url_from_prometheus:
                    parsed_am_url_from_prometheus = urlparse(am_url_from_prometheus)
                    # Compare scheme, hostname, and port
                    if (parsed_am_url_from_prometheus.scheme == parsed_expected_am_url.scheme and
                        parsed_am_url_from_prometheus.hostname == parsed_expected_am_url.hostname and
                        parsed_am_url_from_prometheus.port == parsed_expected_am_url.port):
                        print_status(f"Prometheus: Successfully connected to Alertmanager at {am_url_from_prometheus}.", True)
                        return True
            
            print_status(f"Prometheus: Expected Alertmanager ({alertmanager_url}) not found among active Alertmanagers: {[am.get('url') for am in active_alertmanagers]}.", False)
            return False
        else:
            print_status(f"Prometheus: API request to {alertmanagers_api_url} was not successful (status: {alertmanagers_data.get('status')}).", False)
            return False

    except requests.exceptions.RequestException as e:
        print_status(f"Prometheus: Error querying alertmanagers API at {alertmanagers_api_url}: {e}", False)
        return False
    except json.JSONDecodeError:
        print_status(f"Prometheus: Could not decode JSON response from {alertmanagers_api_url}.", False)
        return False

def main():
    parser = argparse.ArgumentParser(description="SRE Stack Health Checker for EnhancedStatusAggregator.")
    parser.add_argument("--app-url", default=DEFAULT_APP_URL, help=f"URL of the EnhancedStatusAggregator app (default: {DEFAULT_APP_URL})")
    parser.add_argument("--prometheus-url", default=DEFAULT_PROMETHEUS_URL, help=f"URL of the Prometheus server (default: {DEFAULT_PROMETHEUS_URL})")
    parser.add_argument("--alertmanager-url", default=DEFAULT_ALERTMANAGER_URL, help=f"URL of the Alertmanager server (default: {DEFAULT_ALERTMANAGER_URL})")
    parser.add_argument("--app-job-name", default=DEFAULT_APP_JOB_NAME_IN_PROMETHEUS, help=f"Prometheus job name for the app (default: {DEFAULT_APP_JOB_NAME_IN_PROMETHEUS})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})")
    
    args = parser.parse_args()

    print(f"{Colors.YELLOW}--- SRE Stack Health Checker ---{Colors.ENDC}")
    print(f"Using App URL: {args.app_url}")
    print(f"Using Prometheus URL: {args.prometheus_url}")
    print(f"Using Alertmanager URL: {args.alertmanager_url}")
    print(f"Expected App Job Name in Prometheus: {args.app_job_name}")
    print(f"Request Timeout: {args.timeout}s\n")

    results = {}
    all_ok = True

    # Check 1: Application Health
    results["App Health"] = check_endpoint("EnhancedStatusAggregator App", f"{args.app_url}/health", timeout=args.timeout)
    if not results["App Health"]: all_ok = False

    # Check 2: Prometheus Server Health
    results["Prometheus Health"] = check_endpoint("Prometheus Server", f"{args.prometheus_url}/-/healthy", timeout=args.timeout)
    if not results["Prometheus Health"]: all_ok = False

    # Check 3: Prometheus Scraping Application Target (only if Prometheus is healthy)
    if results.get("Prometheus Health"):
        results["Prometheus Scrapes App"] = check_prometheus_app_target(args.prometheus_url, args.app_job_name, args.app_url, timeout=args.timeout)
        if not results["Prometheus Scrapes App"]: all_ok = False
    else:
        results["Prometheus Scrapes App"] = False # Cannot check if Prometheus is down
        print_status("Prometheus: Skipping app target check as Prometheus server is down.", False)
        all_ok = False


    # Check 4: Alertmanager Server Health
    results["Alertmanager Health"] = check_endpoint("Alertmanager Server", f"{args.alertmanager_url}/-/healthy", timeout=args.timeout)
    if not results["Alertmanager Health"]: all_ok = False

    # Check 5: Prometheus to Alertmanager Link (only if Prometheus is healthy)
    if results.get("Prometheus Health"):
        results["Prometheus to Alertmanager Link"] = check_prometheus_alertmanager_link(args.prometheus_url, args.alertmanager_url, timeout=args.timeout)
        if not results["Prometheus to Alertmanager Link"]: all_ok = False
    else:
        results["Prometheus to Alertmanager Link"] = False # Cannot check if Prometheus is down
        print_status("Prometheus: Skipping Alertmanager link check as Prometheus server is down.", False)
        all_ok = False

    print(f"\n{Colors.YELLOW}--- Summary ---{Colors.ENDC}")
    for check_name, status in results.items():
        status_text = f"{Colors.GREEN}OK{Colors.ENDC}" if status else f"{Colors.RED}PROBLEM{Colors.ENDC}"
        print(f"{check_name}: {status_text}")
    
    if all_ok:
        print(f"\n{Colors.GREEN}All checks passed. SRE stack appears healthy.{Colors.ENDC}")
    else:
        print(f"\n{Colors.RED}One or more checks failed. Please review logs above.{Colors.ENDC}")

if __name__ == "__main__":
    main()

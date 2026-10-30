import os
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

def launch_login_browser():
    profile_dir = os.path.abspath("./chrome_profile_buster")
    print(f"Profile directory: {profile_dir}")

    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    print("\n" + "="*60)
    print("ACTION REQUIRED:")
    print("1. The browser is now opening to Glassdoor.")
    print("2. Log in using your Google account manually.")
    print("3. Verify you can see your profile / avatar on Glassdoor.")
    print("4. When fully logged in, come back here and press ENTER to save.")
    print("="*60 + "\n")

    driver.get("https://www.glassdoor.co.in/index.htm")
    input("Press ENTER here after you have successfully logged in in the browser...")

    driver.quit()
    print("\n[SUCCESS] Session saved in './chrome_profile_buster'! The main bot will now be logged in automatically.")

if __name__ == "__main__":
    launch_login_browser()
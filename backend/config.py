from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    mongo_uri: str
    gemini_api_key: str
    elevenlabs_api_key: str
    elevenlabs_agent_id: str

    # ------------------------------------------------------------------ #
    # Gmail SMTP — active notification backend                            #
    # ------------------------------------------------------------------ #
    gmail_user:              str   # e.g. you@gmail.com
    gmail_app_password:      str   # 16-char App Password from Google Account
    emergency_contact_email: str   # recipient of all emergency / safe emails

    # ------------------------------------------------------------------ #
    # Twilio SMS — kept so swapping back requires minimal changes         #
    # Set these if you want to revert to SMS; not required for Gmail mode #
    # ------------------------------------------------------------------ #
    twilio_account_sid:      str = ""
    twilio_auth_token:       str = ""
    twilio_from_number:      str = ""
    emergency_contact_number: str = ""


settings = Settings()

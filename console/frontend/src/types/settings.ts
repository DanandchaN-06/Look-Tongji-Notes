export type SettingsGroup =
  | 'account'
  | 'asr'
  | 'note'
  | 'text_model'
  | 'vision_model'
  | 'slides'
  | 'tasks'
  | 'reader';

export interface SettingsData {
  revision_id: string | null;
  account: {
    username: string;
    username_set: boolean;
    password_set: boolean;
    workspace_root: string;
    owner_name: string;
    site_name: string;
  };
  asr: {
    provider: 'bcutasr';
    availability: 'unknown' | 'available' | 'unavailable';
    upload_target: string;
    providers: Array<{
      id: string;
      name: string;
      availability: 'unknown' | 'available' | 'unavailable';
      upload_target: string;
      reason?: string;
    }>;
  };
  note: {
    route: 'external_agent' | 'model';
    style: 'standard' | 'dialogue';
    include_timeline: boolean;
  };
  text_model: ModelSettings & { providers: string[] };
  vision_model: ModelSettings & { providers: Array<{ id: string; name: string; baseUrl?: string; models?: string[] }> };
  slides: {
    concurrency: number;
    retries: number;
    timeout_seconds: number;
    allow_without_screenshots: boolean;
  };
  tasks: { course_parallelism: number; asr_parallelism: 1; retry_limit: number };
  reader: { auto_update: boolean; publish_repository: string };
}

export interface ModelSettings {
  provider: string;
  model: string;
  base_url: string;
  configured: boolean;
  key_configured: boolean;
}

export interface SettingsDraft {
  account: SettingsData['account'] & { password: string };
  asr: SettingsData['asr'];
  note: SettingsData['note'];
  text_model: SettingsData['text_model'] & { api_key: string; clear_api_key: boolean };
  vision_model: SettingsData['vision_model'] & { api_key: string };
  slides: SettingsData['slides'];
  tasks: SettingsData['tasks'];
  reader: SettingsData['reader'];
}

# coding:utf-8
from enum import Enum

from app.core.utils.platform_utils import (
    MLX_SUPPORTED,
    COOKIE_BROWSER_OPTIONS,
    DEFAULT_COOKIE_BROWSER_LABEL,
)

from PyQt5.QtCore import QLocale
from PyQt5.QtGui import QColor
from qfluentwidgets import (
    qconfig,
    QConfig,
    ConfigItem,
    OptionsConfigItem,
    BoolValidator,
    OptionsValidator,
    RangeConfigItem,
    RangeValidator,
    Theme,
    FolderValidator,
    ConfigSerializer,
    EnumSerializer,
)

from app.config import WORK_PATH, SETTINGS_PATH, MODEL_PATH, WHISPERX_ONLY_MODE
from ..core.entities import (
    LLMServiceEnum,
    SplitTypeEnum,
    TargetLanguageEnum,
    TranscribeModelEnum,
    TranscribeLanguageEnum,
)
from ..core.utils.mlx_model_utils import DEFAULT_MLX_MODEL, preferred_mlx_model


TRANSCRIBE_MODEL_OPTIONS = (
    (
        [TranscribeModelEnum.WHISPER_X, TranscribeModelEnum.MLX_WHISPER]
        if MLX_SUPPORTED
        else [TranscribeModelEnum.WHISPER_X]
    )
    if WHISPERX_ONLY_MODE
    else list(TranscribeModelEnum)
)
DEFAULT_TRANSCRIBE_MODEL = (
    TranscribeModelEnum.WHISPER_X
    if WHISPERX_ONLY_MODE
    else TranscribeModelEnum.WHISPER_X
)
WHISPERX_DEVICE_OPTIONS = ["cpu"] if WHISPERX_ONLY_MODE else ["cuda", "cpu"]
DEFAULT_WHISPERX_DEVICE = "cpu" if WHISPERX_ONLY_MODE else "cuda"
DEFAULT_WHISPERX_COMPUTE_TYPE = "int8" if WHISPERX_ONLY_MODE else "float16"
DEFAULT_WHISPERX_WORD_TIMESTAMPS = True if WHISPERX_ONLY_MODE else False


class Language(Enum):
    """软件语言"""

    CHINESE_SIMPLIFIED = QLocale(QLocale.Chinese, QLocale.China)
    CHINESE_TRADITIONAL = QLocale(QLocale.Chinese, QLocale.HongKong)
    ENGLISH = QLocale(QLocale.English)
    AUTO = QLocale()


class SubtitleLayoutEnum(Enum):
    """字幕布局"""

    TRANSLATE_ON_TOP = "译文在上"
    ORIGINAL_ON_TOP = "原文在上"
    ONLY_ORIGINAL = "仅原文"
    ONLY_TRANSLATE = "仅译文"
    SEPARATE_ORIGINAL_TRANSLATE = "单独输出原文和译文"


class LanguageSerializer(ConfigSerializer):
    """Language serializer"""

    def serialize(self, language):
        return language.value.name() if language != Language.AUTO else "Auto"

    def deserialize(self, value: str):
        return Language(QLocale(value)) if value != "Auto" else Language.AUTO


LEGACY_LANGUAGE_DISPLAY_NAMES = {
    "English": "英语",
    "Chinese": "中文",
    "French": "法语",
    "Russian": "俄语",
    "Arabic": "阿拉伯语",
    "Spanish": "西班牙语",
    "Japanese": "日本語",
    "German": "德语",
    "Korean": "韩语",
    "简体中文": "中文",
    "繁体中文": "中文",
    "粤语": "中文",
    "Polish": "波兰语",
    "Catalan": "加泰罗尼亚语",
    "Dutch": "荷兰语",
    "Arabic": "阿拉伯语",
    "Swedish": "瑞典语",
    "Italian": "意大利语",
    "Indonesian": "印尼语",
    "Hindi": "印地语",
    "Finnish": "芬兰语",
    "Vietnamese": "越南语",
    "Hebrew": "希伯来语",
    "Ukrainian": "乌克兰语",
    "Greek": "希腊语",
    "Malay": "马来语",
    "Czech": "捷克语",
    "Romanian": "罗马尼亚语",
    "Danish": "丹麦语",
    "Hungarian": "匈牙利语",
    "Tamil": "泰米尔语",
    "Norwegian": "挪威语",
    "Thai": "泰语",
    "Urdu": "乌尔都语",
    "Croatian": "克罗地亚语",
    "Bulgarian": "保加利亚语",
    "Lithuanian": "立陶宛语",
    "Latin": "拉丁语",
    "Maori": "毛利语",
    "Malayalam": "马拉雅拉姆语",
    "Welsh": "威尔士语",
    "Slovak": "斯洛伐克语",
    "Telugu": "泰卢固语",
    "Persian": "波斯语",
    "Latvian": "拉脱维亚语",
    "Bengali": "孟加拉语",
    "Serbian": "塞尔维亚语",
    "Azerbaijani": "阿塞拜疆语",
    "Slovenian": "斯洛文尼亚语",
    "Kannada": "卡纳达语",
    "Estonian": "爱沙尼亚语",
    "Macedonian": "马其顿语",
    "Breton": "布列塔尼语",
    "Basque": "巴斯克语",
    "Icelandic": "冰岛语",
    "Armenian": "亚美尼亚语",
    "Nepali": "尼泊尔语",
    "Mongolian": "蒙古语",
    "Bosnian": "波斯尼亚语",
    "Kazakh": "哈萨克语",
    "Albanian": "阿尔巴尼亚语",
    "Swahili": "斯瓦希里语",
    "Galician": "加利西亚语",
    "Marathi": "马拉地语",
    "Punjabi": "旁遮普语",
    "Sinhala": "僧伽罗语",
    "Khmer": "高棉语",
    "Shona": "绍纳语",
    "Yoruba": "约鲁巴语",
    "Somali": "索马里语",
    "Afrikaans": "南非荷兰语",
    "Occitan": "奥克语",
    "Georgian": "格鲁吉亚语",
    "Belarusian": "白俄罗斯语",
    "Tajik": "塔吉克语",
    "Sindhi": "信德语",
    "Gujarati": "古吉拉特语",
    "Amharic": "阿姆哈拉语",
    "Yiddish": "意第绪语",
    "Lao": "老挝语",
    "Uzbek": "乌兹别克语",
    "Faroese": "法罗语",
    "Haitian Creole": "海地克里奥尔语",
    "Pashto": "普什图语",
    "Turkmen": "土库曼语",
    "Nynorsk": "新挪威语",
    "Maltese": "马耳他语",
    "Sanskrit": "梵语",
    "Luxembourgish": "卢森堡语",
    "Myanmar": "缅甸语",
    "Tibetan": "藏语",
    "Tagalog": "他加禄语",
    "Malagasy": "马达加斯加语",
    "Assamese": "阿萨姆语",
    "Tatar": "鞑靼语",
    "Hawaiian": "夏威夷语",
    "Lingala": "林加拉语",
    "Hausa": "豪萨语",
    "Bashkir": "巴什基尔语",
    "Javanese": "爪哇语",
    "Sundanese": "巽他语",
    "Cantonese": "粤语（广东话）",
}


class LegacyEnumSerializer(ConfigSerializer):
    def __init__(self, enum_class, legacy_values=None, fallback=None):
        self.enum_class = enum_class
        self.legacy_values = legacy_values or {}
        self.fallback = fallback or next(iter(enum_class))

    def serialize(self, value):
        return value.value

    def deserialize(self, value):
        try:
            return self.enum_class(self.legacy_values.get(value, value))
        except ValueError:
            return self.fallback


class Config(QConfig):
    """应用配置"""

    # LLM配置
    llm_service = OptionsConfigItem(
        "LLM",
        "LLMService",
        LLMServiceEnum.PUBLIC,
        OptionsValidator(LLMServiceEnum),
        EnumSerializer(LLMServiceEnum),
    )

    openai_model = ConfigItem("LLM", "OpenAI_Model", "gpt-4o-mini")
    openai_api_key = ConfigItem("LLM", "OpenAI_API_Key", "")
    openai_api_base = ConfigItem("LLM", "OpenAI_API_Base", "https://api.openai.com/v1")

    silicon_cloud_model = ConfigItem("LLM", "SiliconCloud_Model", "gpt-4o-mini")
    silicon_cloud_api_key = ConfigItem("LLM", "SiliconCloud_API_Key", "")
    silicon_cloud_api_base = ConfigItem(
        "LLM", "SiliconCloud_API_Base", "https://api.siliconflow.cn/v1"
    )

    deepseek_model = ConfigItem("LLM", "DeepSeek_Model", "v4-pro")
    deepseek_api_key = ConfigItem("LLM", "DeepSeek_API_Key", "")
    deepseek_api_base = ConfigItem(
        "LLM", "DeepSeek_API_Base", "https://api.deepseek.com/v1"
    )

    ollama_model = ConfigItem("LLM", "Ollama_Model", "llama2")
    ollama_api_key = ConfigItem("LLM", "Ollama_API_Key", "ollama")
    ollama_api_base = ConfigItem("LLM", "Ollama_API_Base", "http://localhost:11434/v1")

    lm_studio_model = ConfigItem("LLM", "LmStudio_Model", "qwen2.5:7b")
    lm_studio_api_key = ConfigItem("LLM", "LmStudio_API_Key", "lmstudio")
    lm_studio_api_base = ConfigItem(
        "LLM", "LmStudio_API_Base", "http://localhost:1234/v1"
    )

    gemini_model = ConfigItem("LLM", "Gemini_Model", "gemini-pro")
    gemini_api_key = ConfigItem("LLM", "Gemini_API_Key", "")
    gemini_api_base = ConfigItem(
        "LLM",
        "Gemini_API_Base",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    )

    chatglm_model = ConfigItem("LLM", "ChatGLM_Model", "glm-4")
    chatglm_api_key = ConfigItem("LLM", "ChatGLM_API_Key", "")
    chatglm_api_base = ConfigItem(
        "LLM", "ChatGLM_API_Base", "https://open.bigmodel.cn/api/paas/v4"
    )

    # 公益模型
    qwen_model = ConfigItem("LLM", "Qwen_Model", "qwen-plus")
    qwen_api_key = ConfigItem("LLM", "Qwen_API_Key", "")
    qwen_api_base = ConfigItem(
        "LLM", "Qwen_API_Base", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    qwen_enable_thinking = ConfigItem(
        "LLM", "Qwen_Enable_Thinking", True, BoolValidator()
    )

    public_model = ConfigItem("LLM", "Public_Model", "gpt-4o-mini")
    public_api_key = ConfigItem(
        "LLM", "Public_API_Key", "please-do-not-use-for-personal-purposes"
    )
    public_api_base = ConfigItem("LLM", "Public_API_Base", "https://ddg.bkfeng.top/v1")
    llm_request_timeout = RangeConfigItem(
        "LLM", "RequestTimeout", 300, RangeValidator(30, 900)
    )
    llm_cache_enabled = ConfigItem("LLM", "CacheEnabled", True, BoolValidator())
    llm_batch_context_enabled = ConfigItem(
        "LLM", "BatchContextEnabled", True, BoolValidator()
    )
    llm_batch_context_max_chars = RangeConfigItem(
        "LLM", "BatchContextMaxChars", 300, RangeValidator(0, 1000)
    )

    # ------------------- 翻译配置 -------------------
    need_reflect_translate = ConfigItem(
        "Translate", "NeedReflectTranslate", False, BoolValidator()
    )
    batch_size = RangeConfigItem("Translate", "BatchSize", 10, RangeValidator(5, 30))
    thread_num = RangeConfigItem("Translate", "ThreadNum", 10, RangeValidator(1, 100))
    translation_max_length = RangeConfigItem(
        "Translate", "TranslationMaxLength", 0, RangeValidator(0, 80)
    )
    final_translation_rework_max_chars = RangeConfigItem(
        "Translate", "FinalTranslationReworkMaxChars", 40, RangeValidator(0, 120)
    )

    # ------------------- 转录配置 -------------------
    transcribe_model = OptionsConfigItem(
        "Transcribe",
        "TranscribeModel",
        DEFAULT_TRANSCRIBE_MODEL,
        OptionsValidator(TRANSCRIBE_MODEL_OPTIONS),
        EnumSerializer(TranscribeModelEnum),
    )
    use_asr_cache = ConfigItem("Transcribe", "UseASRCache", True, BoolValidator())
    transcribe_language = OptionsConfigItem(
        "Transcribe",
        "TranscribeLanguage",
        TranscribeLanguageEnum.ENGLISH,
        OptionsValidator(TranscribeLanguageEnum),
        LegacyEnumSerializer(
            TranscribeLanguageEnum,
            LEGACY_LANGUAGE_DISPLAY_NAMES,
            TranscribeLanguageEnum.ENGLISH,
        ),
    )

    # ------------------- WhisperX 配置 -------------------
    whisperx_model = ConfigItem("WhisperX", "Model", "large-v3-turbo")
    whisperx_device = OptionsConfigItem(
        "WhisperX",
        "Device",
        DEFAULT_WHISPERX_DEVICE,
        OptionsValidator(WHISPERX_DEVICE_OPTIONS),
    )
    whisperx_compute_type = ConfigItem(
        "WhisperX", "ComputeType", DEFAULT_WHISPERX_COMPUTE_TYPE
    )
    whisperx_batch_size = RangeConfigItem(
        "WhisperX", "BatchSize", 8, RangeValidator(1, 32)
    )
    whisperx_auto_language = ConfigItem(
        "WhisperX", "AutoLanguage", False, BoolValidator()
    )
    whisperx_hotwords = ConfigItem("WhisperX", "Hotwords", "")
    whisperx_initial_prompt = ConfigItem("WhisperX", "InitialPrompt", "")
    whisperx_vad_method = OptionsConfigItem(
        "WhisperX",
        "VadMethod",
        "silero",
        OptionsValidator(["silero", "pyannote"]),
    )
    whisperx_vad_threshold = ConfigItem("WhisperX", "VadThreshold", 0.5)
    whisperx_local_silero_dir = ConfigItem(
        "WhisperX",
        "LocalSileroDir",
        str(MODEL_PATH / "silero-vad"),
    )
    whisperx_word_timestamps = ConfigItem(
        "WhisperX",
        "WordTimestamps",
        DEFAULT_WHISPERX_WORD_TIMESTAMPS,
        BoolValidator(),
    )
    whisperx_align = ConfigItem("WhisperX", "Align", True, BoolValidator())
    # ------------------- MLX Whisper 配置 -------------------
    mlx_model = ConfigItem(
        "MLXWhisper",
        "Model",
        DEFAULT_MLX_MODEL,
    )
    mlx_word_timestamps = ConfigItem(
        "MLXWhisper",
        "WordTimestamps",
        True,
        BoolValidator(),
    )
    mlx_hotwords = ConfigItem("MLXWhisper", "Hotwords", "")
    mlx_initial_prompt = ConfigItem("MLXWhisper", "InitialPrompt", "")
    mlx_vad_enabled = ConfigItem("MLXWhisper", "VadEnabled", True, BoolValidator())
    mlx_vad_threshold = ConfigItem("MLXWhisper", "VadThreshold", 0.5)
    mlx_chunk_duration = RangeConfigItem(
        "MLXWhisper", "ChunkDuration", 600, RangeValidator(60, 1800)
    )
    mlx_chunk_overlap = RangeConfigItem(
        "MLXWhisper", "ChunkOverlap", 30, RangeValidator(0, 300)
    )
    # ------------------- 字幕配置 -------------------
    need_optimize = ConfigItem("Subtitle", "NeedOptimize", False, BoolValidator())
    need_translate = ConfigItem("Subtitle", "NeedTranslate", False, BoolValidator())
    need_split = ConfigItem("Subtitle", "NeedSplit", False, BoolValidator())
    split_type = OptionsConfigItem(
        "Subtitle",
        "SplitType",
        SplitTypeEnum.SENTENCE,
        OptionsValidator(SplitTypeEnum),
        EnumSerializer(SplitTypeEnum),
    )
    target_language = OptionsConfigItem(
        "Subtitle",
        "TargetLanguage",
        TargetLanguageEnum.CHINESE,
        OptionsValidator(TargetLanguageEnum),
        LegacyEnumSerializer(
            TargetLanguageEnum,
            LEGACY_LANGUAGE_DISPLAY_NAMES,
            TargetLanguageEnum.CHINESE,
        ),
    )
    max_word_count_cjk = ConfigItem(
        "Subtitle", "MaxWordCountCJK", 25, RangeValidator(8, 100)
    )
    max_word_count_english = ConfigItem(
        "Subtitle", "MaxWordCountEnglish", 20, RangeValidator(8, 100)
    )
    needs_remove_translated_chinese_commas = ConfigItem(
        "Subtitle", "NeedsRemoveTranslatedChineseCommas", False, BoolValidator()
    )
    needs_remove_punctuation = ConfigItem(
        "Subtitle", "NeedsRemovePunctuation", True, BoolValidator()
    )
    need_mask_original_profanity = ConfigItem(
        "Subtitle", "NeedMaskOriginalProfanity", False, BoolValidator()
    )
    custom_prompt_text = ConfigItem("Subtitle", "CustomPromptText", "")
    desktop_notifications_enabled = ConfigItem(
        "General", "DesktopNotificationsEnabled", True, BoolValidator()
    )

    # ------------------- 提示词中心 -------------------
    prompt_split_semantic = ConfigItem("PromptCenter", "SplitSemantic", "")
    prompt_split_sentence = ConfigItem("PromptCenter", "SplitSentence", "")
    prompt_split_sentence_restore = ConfigItem(
        "PromptCenter", "SplitSentenceRestore", ""
    )
    prompt_summarizer = ConfigItem("PromptCenter", "Summarizer", "")
    prompt_optimizer = ConfigItem("PromptCenter", "Optimizer", "")
    prompt_translate = ConfigItem("PromptCenter", "Translate", "")
    prompt_reflect_translate = ConfigItem("PromptCenter", "ReflectTranslate", "")
    prompt_single_translate = ConfigItem("PromptCenter", "SingleTranslate", "")
    prompt_term_glossary = ConfigItem("PromptCenter", "TermGlossary", "")

    # ------------------- 字幕布局配置 -------------------
    subtitle_layout = OptionsConfigItem(
        "Subtitle",
        "Layout",
        "译文在上",
        OptionsValidator(
            ["译文在上", "原文在上", "仅译文", "仅原文", "单独输出原文和译文"]
        ),
    )

    # ------------------- 保存配置 -------------------
    work_dir = ConfigItem("Save", "Work_Dir", WORK_PATH, FolderValidator())

    # ------------------- 下载设置 -------------------
    download_center_output_dir = ConfigItem("Download", "CenterOutputDir", "")
    download_engine_strategy = OptionsConfigItem(
        "Download",
        "EngineStrategy",
        "智能选择",
        OptionsValidator(["单线程", "多线程", "智能选择"]),
    )
    download_native_hevc_preset = OptionsConfigItem(
        "Download",
        "NativeHevcPreset",
        "highest_quality",
        OptionsValidator(["fast_1080p", "balanced_4k", "highest_quality"]),
    )
    # Keep the legacy persisted key so existing users retain their switch value.
    download_auto_extract_cookies_on_startup = ConfigItem(
        "Download", "AutoRefreshEdgeCookies", False, BoolValidator()
    )
    download_cookie_browser = OptionsConfigItem(
        "Download",
        "CookieBrowser",
        DEFAULT_COOKIE_BROWSER_LABEL,
        OptionsValidator(COOKIE_BROWSER_OPTIONS),
    )
    download_center_mode = OptionsConfigItem(
        "Download",
        "CenterMode",
        "simple",
        OptionsValidator(["simple", "professional"]),
    )
    download_center_simple_preset = OptionsConfigItem(
        "Download",
        "CenterSimplePreset",
        "best_quality",
        OptionsValidator(
            [
                "best_quality",
                "mp4_compatible",
                "pr_smart",
                "custom_preferences",
                "audio_only",
                "subtitle_only",
                "thumbnail_only",
            ]
        ),
    )
    download_center_professional_mode = OptionsConfigItem(
        "Download",
        "CenterProfessionalMode",
        "video_audio",
        OptionsValidator(["video_audio", "video", "audio"]),
    )
    download_center_need_subtitle = ConfigItem(
        "Download", "CenterNeedSubtitle", False, BoolValidator()
    )
    download_center_need_thumbnail = ConfigItem(
        "Download", "CenterNeedThumbnail", True, BoolValidator()
    )
    download_center_need_metadata = ConfigItem(
        "Download", "CenterNeedMetadata", False, BoolValidator()
    )
    download_center_need_description_txt = ConfigItem(
        "Download", "CenterNeedDescriptionTxt", True, BoolValidator()
    )
    download_description_txt_template = ConfigItem(
        "Download", "DescriptionTxtTemplate", ""
    )
    download_center_subtitle_mode = OptionsConfigItem(
        "Download",
        "CenterSubtitleMode",
        "manual",
        OptionsValidator(["manual", "auto"]),
    )
    download_center_subtitle_language = ConfigItem(
        "Download", "CenterSubtitleLanguage", "en"
    )
    download_center_custom_video_codec = OptionsConfigItem(
        "Download",
        "CenterCustomVideoCodec",
        "auto",
        OptionsValidator(["auto", "avc1", "av01", "vp9"]),
    )
    download_center_custom_container = OptionsConfigItem(
        "Download",
        "CenterCustomContainer",
        "auto",
        OptionsValidator(["auto", "mp4", "webm"]),
    )
    download_center_custom_audio_codec = OptionsConfigItem(
        "Download",
        "CenterCustomAudioCodec",
        "auto",
        OptionsValidator(["auto", "mp4a", "opus"]),
    )
    download_center_pr_smart_postprocess = ConfigItem(
        "Download", "CenterPRSmartPostprocess", False, BoolValidator()
    )
    download_center_pr_smart_transcript_txt = ConfigItem(
        "Download", "CenterPRSmartTranscriptTxt", False, BoolValidator()
    )

    # ------------------- 软件页面配置 -------------------
    dpiScale = OptionsConfigItem(
        "MainWindow",
        "DpiScale",
        "Auto",
        OptionsValidator([1, 1.25, 1.5, 1.75, 2, "Auto"]),
        restart=True,
    )
    language = OptionsConfigItem(
        "MainWindow",
        "Language",
        Language.AUTO,
        OptionsValidator(Language),
        LanguageSerializer(),
        restart=True,
    )

    # ------------------- 下载代理配置 -------------------
    download_proxy_mode = OptionsConfigItem(
        "Download",
        "ProxyMode",
        "自动检测",
        OptionsValidator(["自动检测", "手动设置", "不使用代理"]),
    )
    download_proxy_url = ConfigItem(
        "Download",
        "ProxyURL",
        "http://127.0.0.1:7897",
    )


cfg = Config()
cfg.themeMode.value = Theme.DARK
cfg.themeColor.value = QColor("#ff28f08b")
qconfig.load(SETTINGS_PATH, cfg)

cfg.mlx_model.value = preferred_mlx_model(cfg.mlx_model.value)

if WHISPERX_ONLY_MODE and cfg.transcribe_model.value == TranscribeModelEnum.WHISPER_X:
    cfg.set(cfg.whisperx_device, "cpu")
    if cfg.whisperx_compute_type.value in {"float16", "int8_float16"}:
        cfg.set(cfg.whisperx_compute_type, "int8")
    cfg.set(cfg.whisperx_word_timestamps, True)
    cfg.set(cfg.whisperx_align, True)

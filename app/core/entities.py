import datetime
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SupportedAudioFormats(Enum):
    """支持的音频格式"""

    AAC = "aac"
    AC3 = "ac3"
    AIFF = "aiff"
    AMR = "amr"
    APE = "ape"
    AU = "au"
    FLAC = "flac"
    M4A = "m4a"
    MP2 = "mp2"
    MP3 = "mp3"
    MKA = "mka"
    OGA = "oga"
    OGG = "ogg"
    OPUS = "opus"
    RA = "ra"
    WAV = "wav"
    WMA = "wma"


class SupportedVideoFormats(Enum):
    """支持的视频格式"""

    MP4 = "mp4"
    WEBM = "webm"
    OGM = "ogm"
    MOV = "mov"
    MKV = "mkv"
    AVI = "avi"
    WMV = "wmv"
    FLV = "flv"
    M4V = "m4v"
    TS = "ts"
    MPG = "mpg"
    MPEG = "mpeg"
    VOB = "vob"
    ASF = "asf"
    RM = "rm"
    RMVB = "rmvb"
    M2TS = "m2ts"
    MTS = "mts"
    DV = "dv"
    GXF = "gxf"
    TOD = "tod"
    MXF = "mxf"
    F4V = "f4v"


class SupportedSubtitleFormats(Enum):
    """支持的字幕格式"""

    SRT = "srt"
    VTT = "vtt"


class OutputSubtitleFormatEnum(Enum):
    """字幕输出格式"""

    SRT = "srt"
    VTT = "vtt"
    JSON = "json"
    TXT = "txt"


class LLMServiceEnum(Enum):
    """LLM服务"""

    OPENAI = "OpenAI"
    SILICON_CLOUD = "SiliconCloud"
    DEEPSEEK = "DeepSeek"
    OLLAMA = "Ollama"
    LM_STUDIO = "LM Studio"
    GEMINI = "Gemini"
    CHATGLM = "ChatGLM"
    QWEN = "Qwen"
    PUBLIC = "软件公益模型"


class TranscribeModelEnum(Enum):
    """转录模型"""

    WHISPER_X = "WhisperX"
    MLX_WHISPER = "MLX Whisper"


class VadMethodEnum(Enum):
    """VAD方法"""

    SILERO_V3 = "silero_v3"  # 通常比 v4 准确性低，但没有 v4 的一些怪癖
    SILERO_V4 = (
        "silero_v4"  # 与 silero_v4_fw 相同。运行原始 Silero 的代码，而不是适配过的代码
    )
    SILERO_V5 = (
        "silero_v5"  # 与 silero_v5_fw 相同。运行原始 Silero 的代码，而不是适配过的代码)
    )
    SILERO_V4_FW = (
        "silero_v4_fw"  # 默认模型。最准确的 Silero 版本，有一些非致命的小问题
    )
    # SILERO_V5_FW = "silero_v5_fw"  # 准确性差。不是 VAD，而是某种语音的随机检测器，有各种致命的小问题。避免使用！
    PYANNOTE_V3 = "pyannote_v3"  # 最佳准确性，支持 CUDA
    PYANNOTE_ONNX_V3 = "pyannote_onnx_v3"  # pyannote_v3 的轻量版。与 Silero v4 的准确性相似，可能稍好，支持 CUDA
    WEBRTC = "webrtc"  # 准确性低，过时的 VAD。仅接受 'vad_min_speech_duration_ms' 和 'vad_speech_pad_ms'
    AUDITOK = "auditok"  # 实际上这不是 VAD，而是 AAD - 音频活动检测


class SplitTypeEnum(Enum):
    """字幕分段类型"""

    SEMANTIC = "语义分段"
    SENTENCE = "句子分段"


class TargetLanguageEnum(Enum):
    """翻译目标语言"""

    ENGLISH = "英语"
    CHINESE = "中文"
    JAPANESE = "日本語"
    KOREAN = "韩语"
    FRENCH = "法语"
    GERMAN = "德语"
    SPANISH = "西班牙语"
    RUSSIAN = "俄语"
    ARABIC = "阿拉伯语"


class TranscribeLanguageEnum(Enum):
    """转录语言"""

    ENGLISH = "英语"
    CHINESE = "中文"
    JAPANESE = "日本語"
    KOREAN = "韩语"
    FRENCH = "法语"
    GERMAN = "德语"
    SPANISH = "西班牙语"
    RUSSIAN = "俄语"
    ARABIC = "阿拉伯语"


LANGUAGES = {
    "英语": "en",
    "中文": "zh",
    "日本語": "ja",
    "德语": "de",
    "粤语": "yue",
    "西班牙语": "es",
    "俄语": "ru",
    "韩语": "ko",
    "法语": "fr",
    "葡萄牙语": "pt",
    "土耳其语": "tr",
    "波兰语": "pl",
    "加泰罗尼亚语": "ca",
    "荷兰语": "nl",
    "阿拉伯语": "ar",
    "瑞典语": "sv",
    "意大利语": "it",
    "印尼语": "id",
    "印地语": "hi",
    "芬兰语": "fi",
    "越南语": "vi",
    "希伯来语": "he",
    "乌克兰语": "uk",
    "希腊语": "el",
    "马来语": "ms",
    "捷克语": "cs",
    "罗马尼亚语": "ro",
    "丹麦语": "da",
    "匈牙利语": "hu",
    "泰米尔语": "ta",
    "挪威语": "no",
    "泰语": "th",
    "乌尔都语": "ur",
    "克罗地亚语": "hr",
    "保加利亚语": "bg",
    "立陶宛语": "lt",
    "拉丁语": "la",
    "毛利语": "mi",
    "马拉雅拉姆语": "ml",
    "威尔士语": "cy",
    "斯洛伐克语": "sk",
    "泰卢固语": "te",
    "波斯语": "fa",
    "拉脱维亚语": "lv",
    "孟加拉语": "bn",
    "塞尔维亚语": "sr",
    "阿塞拜疆语": "az",
    "斯洛文尼亚语": "sl",
    "卡纳达语": "kn",
    "爱沙尼亚语": "et",
    "马其顿语": "mk",
    "布列塔尼语": "br",
    "巴斯克语": "eu",
    "冰岛语": "is",
    "亚美尼亚语": "hy",
    "尼泊尔语": "ne",
    "蒙古语": "mn",
    "波斯尼亚语": "bs",
    "哈萨克语": "kk",
    "阿尔巴尼亚语": "sq",
    "斯瓦希里语": "sw",
    "加利西亚语": "gl",
    "马拉地语": "mr",
    "旁遮普语": "pa",
    "僧伽罗语": "si",
    "高棉语": "km",
    "绍纳语": "sn",
    "约鲁巴语": "yo",
    "索马里语": "so",
    "南非荷兰语": "af",
    "奥克语": "oc",
    "格鲁吉亚语": "ka",
    "白俄罗斯语": "be",
    "塔吉克语": "tg",
    "信德语": "sd",
    "古吉拉特语": "gu",
    "阿姆哈拉语": "am",
    "意第绪语": "yi",
    "老挝语": "lo",
    "乌兹别克语": "uz",
    "法罗语": "fo",
    "海地克里奥尔语": "ht",
    "普什图语": "ps",
    "土库曼语": "tk",
    "新挪威语": "nn",
    "马耳他语": "mt",
    "梵语": "sa",
    "卢森堡语": "lb",
    "缅甸语": "my",
    "藏语": "bo",
    "他加禄语": "tl",
    "马达加斯加语": "mg",
    "阿萨姆语": "as",
    "鞑靼语": "tt",
    "夏威夷语": "haw",
    "林加拉语": "ln",
    "豪萨语": "ha",
    "巴什基尔语": "ba",
    "爪哇语": "jw",
    "巽他语": "su",
    "粤语（广东话）": "yue",
    "English": "en",
    "Chinese": "zh",
    "German": "de",
    "Spanish": "es",
    "Russian": "ru",
    "Korean": "ko",
    "French": "fr",
    "Japanese": "ja",
    "Portuguese": "pt",
    "Turkish": "tr",
    "Polish": "pl",
    "Catalan": "ca",
    "Dutch": "nl",
    "Arabic": "ar",
    "Swedish": "sv",
    "Italian": "it",
    "Indonesian": "id",
    "Hindi": "hi",
    "Finnish": "fi",
    "Vietnamese": "vi",
    "Hebrew": "he",
    "Ukrainian": "uk",
    "Greek": "el",
    "Malay": "ms",
    "Czech": "cs",
    "Romanian": "ro",
    "Danish": "da",
    "Hungarian": "hu",
    "Tamil": "ta",
    "Norwegian": "no",
    "Thai": "th",
    "Urdu": "ur",
    "Croatian": "hr",
    "Bulgarian": "bg",
    "Lithuanian": "lt",
    "Latin": "la",
    "Maori": "mi",
    "Malayalam": "ml",
    "Welsh": "cy",
    "Slovak": "sk",
    "Telugu": "te",
    "Persian": "fa",
    "Latvian": "lv",
    "Bengali": "bn",
    "Serbian": "sr",
    "Azerbaijani": "az",
    "Slovenian": "sl",
    "Kannada": "kn",
    "Estonian": "et",
    "Macedonian": "mk",
    "Breton": "br",
    "Basque": "eu",
    "Icelandic": "is",
    "Armenian": "hy",
    "Nepali": "ne",
    "Mongolian": "mn",
    "Bosnian": "bs",
    "Kazakh": "kk",
    "Albanian": "sq",
    "Swahili": "sw",
    "Galician": "gl",
    "Marathi": "mr",
    "Punjabi": "pa",
    "Sinhala": "si",
    "Khmer": "km",
    "Shona": "sn",
    "Yoruba": "yo",
    "Somali": "so",
    "Afrikaans": "af",
    "Occitan": "oc",
    "Georgian": "ka",
    "Belarusian": "be",
    "Tajik": "tg",
    "Sindhi": "sd",
    "Gujarati": "gu",
    "Amharic": "am",
    "Yiddish": "yi",
    "Lao": "lo",
    "Uzbek": "uz",
    "Faroese": "fo",
    "Haitian Creole": "ht",
    "Pashto": "ps",
    "Turkmen": "tk",
    "Nynorsk": "nn",
    "Maltese": "mt",
    "Sanskrit": "sa",
    "Luxembourgish": "lb",
    "Myanmar": "my",
    "Tibetan": "bo",
    "Tagalog": "tl",
    "Malagasy": "mg",
    "Assamese": "as",
    "Tatar": "tt",
    "Hawaiian": "haw",
    "Lingala": "ln",
    "Hausa": "ha",
    "Bashkir": "ba",
    "Javanese": "jw",
    "Sundanese": "su",
    "Cantonese": "yue",
}


@dataclass
class VideoInfo:
    """视频信息类"""

    file_name: str
    file_path: str
    width: int
    height: int
    fps: float
    duration_seconds: float
    bitrate_kbps: int
    video_codec: str
    audio_codec: str
    audio_sampling_rate: int
    thumbnail_path: str


@dataclass
class TranscribeConfig:
    """转录配置类"""

    transcribe_model: Optional[TranscribeModelEnum] = None
    transcribe_language: str = ""
    use_asr_cache: bool = True
    need_word_time_stamp: bool = True
    # WhisperX 配置
    whisperx_model: Optional[str] = None
    whisperx_device: str = "cpu"
    whisperx_compute_type: str = "int8"
    whisperx_batch_size: int = 8
    whisperx_auto_language: bool = False
    whisperx_hotwords: Optional[str] = None
    whisperx_initial_prompt: Optional[str] = None
    whisperx_vad_method: str = "silero"
    whisperx_vad_threshold: float = 0.5
    whisperx_local_silero_dir: Optional[str] = None
    whisperx_align: bool = True
    whisperx_model_dir: Optional[str] = None
    # MLX Whisper 配置
    mlx_model: Optional[str] = None
    mlx_word_timestamps: bool = True
    mlx_hotwords: Optional[str] = None
    mlx_initial_prompt: Optional[str] = None
    mlx_vad_enabled: bool = True
    mlx_vad_threshold: float = 0.5
    mlx_chunk_duration: int = 600
    mlx_chunk_overlap: int = 30


@dataclass
class SubtitleConfig:
    """字幕处理配置类"""

    # 翻译配置
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    llm_model: Optional[str] = None
    llm_service: Optional[str] = None
    qwen_enable_thinking: bool = False
    llm_request_timeout: int = 300
    llm_cache_enabled: bool = True
    llm_batch_context_enabled: bool = True
    llm_batch_context_max_chars: int = 300
    need_translate: bool = False
    need_optimize: bool = False
    need_reflect: bool = False
    thread_num: int = 10
    batch_size: int = 10
    translation_max_length: int = 0
    final_translation_rework_max_chars: int = 40
    # 字幕布局和分割
    split_type: Optional[SplitTypeEnum] = None
    subtitle_layout: Optional[str] = None
    max_word_count_cjk: int = 12
    max_word_count_english: int = 18
    need_split: bool = True
    target_language: Optional[TargetLanguageEnum] = None
    need_remove_translated_chinese_commas: bool = False
    need_remove_punctuation: bool = False
    need_mask_original_profanity: bool = False
    custom_prompt_text: Optional[str] = None


@dataclass
class TranscribeTask:
    """转录任务类"""

    queued_at: Optional[datetime.datetime] = None
    started_at: Optional[datetime.datetime] = None
    completed_at: Optional[datetime.datetime] = None

    # 输入文件
    file_path: Optional[str] = None

    # 输出字幕文件
    output_path: Optional[str] = None

    # 是否需要执行下一个任务（字幕处理）
    need_next_task: bool = False

    transcribe_config: Optional[TranscribeConfig] = None


@dataclass
class SubtitleTask:
    """字幕任务类"""

    queued_at: Optional[datetime.datetime] = None
    started_at: Optional[datetime.datetime] = None
    completed_at: Optional[datetime.datetime] = None

    # 输入原始字幕文件
    subtitle_path: str = ""
    # 输入原始视频文件
    video_path: Optional[str] = None

    # 输出 断句、优化、翻译 后的字幕文件
    output_path: Optional[str] = None

    # 是否需要执行下一个任务（字幕处理）
    need_next_task: bool = True

    subtitle_config: Optional[SubtitleConfig] = None


@dataclass
class TranscriptAndSubtitleTask:
    """转录和字幕任务类"""

    queued_at: Optional[datetime.datetime] = None
    started_at: Optional[datetime.datetime] = None
    completed_at: Optional[datetime.datetime] = None

    # 输入
    file_path: Optional[str] = None

    # 输出
    output_path: Optional[str] = None

    transcribe_config: Optional[TranscribeConfig] = None
    subtitle_config: Optional[SubtitleConfig] = None

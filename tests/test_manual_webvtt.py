from unittest.mock import patch

from app.core.bk_asr.asr_data import ASRData
from app.core.utils.subtitle_transcript import render_transcript_from_subtitle_file
from app.mcp.review import prepare_evidence

# Reduced structure of the uploader's VTT: no cue ids, metadata in the header,
# multiline text and HTML entities. Extra cues exercise standard VTT variants.
MANUAL_VTT = '''WEBVTT
Kind: captions
Language: en

00:00:00.080 --> 00:00:05.440
Do you know what an iceberg is? Those images&nbsp;
where topics are listed at increasingly deeper&nbsp;&nbsp;

NOTE ignored commentary
This is not a cue.

chapter-2
00:05.440 --> 00:09.440 align:start position:10%
<c.highlight>positions</c> on an iceberg, relative to&nbsp;
how obscure &amp; mysterious they are.

STYLE
::cue { color: white; }

00:09.440 --> 00:14.720
&lt;iceberg&gt;
'''


def test_manual_vtt_transcript_and_review_share_all_cues(tmp_path):
    path = tmp_path / 'manual.vtt'
    path.write_text('\ufeff' + MANUAL_VTT.replace('\n', '\r\n'))
    data = ASRData.from_subtitle_file(str(path))
    assert len(data.segments) == 3
    assert [(s.start_time, s.end_time) for s in data.segments] == [(80, 5440), (5440, 9440), (9440, 14720)]
    assert data.segments[1].text == 'positions on an iceberg, relative to how obscure & mysterious they are.'
    transcript = render_transcript_from_subtitle_file(path)
    assert transcript.startswith('Do you know what an iceberg is? Those images where topics')
    assert 'positions on an iceberg' in transcript
    with patch('app.core.bk_asr.mlx_workflow.find_local_silero_repository', return_value=None):
        evidence = prepare_evidence(tmp_path / 'unused.wav', path)
    assert evidence['checks']['source_subtitles'] == {'status': 'checked', 'cue_count': 3}
    assert evidence['source_cues'][2]['text'] == '<iceberg>'


def test_empty_vtt_does_not_claim_review_was_checked(tmp_path):
    path = tmp_path / 'empty.vtt'
    path.write_text('WEBVTT\n\n')
    with patch('app.core.bk_asr.mlx_workflow.find_local_silero_repository', return_value=None):
        evidence = prepare_evidence(tmp_path / 'unused.wav', path)
    assert evidence['checks']['source_subtitles']['status'] == 'unavailable'

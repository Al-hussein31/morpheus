#!/bin/zsh
# Download the local models into models/ (about 1.1 GB). Safe to rerun: skips files you have.
set -e
cd ${0:A:h}/..
mkdir -p models && cd models
SHERPA=https://github.com/k2-fsa/sherpa-onnx/releases/download
KOKORO=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0

get() {  # get URL [FILE]
  local file=${2:-${1:t}}
  if [[ -e $file ]]; then echo "have  $file"; else echo "fetch $file"; curl -fL --progress-bar -o $file.part $1 && mv $file.part $file; fi
}

# Speech to text: NVIDIA Parakeet TDT 0.6B v2 (English), int8, about 630 MB
if [[ ! -d sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8 ]]; then
  get $SHERPA/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2
  tar xjf sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2 && rm sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2
fi
get $SHERPA/asr-models/silero_vad.onnx                                  # finds where speech starts and stops
get $SHERPA/speaker-recongition-models/nemo_en_titanet_small.onnx       # recognises your voice
get $KOKORO/kokoro-v1.0.fp16.onnx                                       # text to speech
get $KOKORO/voices-v1.0.bin
echo "Models ready in $(pwd)"

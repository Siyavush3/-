// Звук микрофона → 16 кГц, 16 бит, моно, кусками по 0,1 с (узел 9). Работает в потоке звука браузера.
// Высокие частоты уже срезаны фильтром перед этим узлом (mic.js), здесь — усреднение и прореживание.
'use strict';

class Pcm16k extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.ratio = options.processorOptions.rate / 16000;
    this.pos = 0;
    this.acc = 0;
    this.n = 0;
    this.buf = new Int16Array(1600);
    this.len = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.acc += ch[i];
      this.n += 1;
      this.pos += 1;
      if (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        const v = Math.max(-1, Math.min(1, this.acc / this.n));
        this.acc = 0;
        this.n = 0;
        this.buf[this.len++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        if (this.len === this.buf.length) {
          this.port.postMessage(this.buf.buffer.slice(0));
          this.len = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('pcm16k', Pcm16k);

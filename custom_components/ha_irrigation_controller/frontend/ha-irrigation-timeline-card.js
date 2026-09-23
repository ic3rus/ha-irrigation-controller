function t(t,e,s,i){var r,n=arguments.length,o=n<3?e:null===i?i=Object.getOwnPropertyDescriptor(e,s):i;if("object"==typeof Reflect&&"function"==typeof Reflect.decorate)o=Reflect.decorate(t,e,s,i);else for(var a=t.length-1;a>=0;a--)(r=t[a])&&(o=(n<3?r(o):n>3?r(e,s,o):r(e,s))||o);return n>3&&o&&Object.defineProperty(e,s,o),o}var e,s;"function"==typeof SuppressedError&&SuppressedError,function(t){t.language="language",t.system="system",t.comma_decimal="comma_decimal",t.decimal_comma="decimal_comma",t.space_comma="space_comma",t.none="none"}(e||(e={})),function(t){t.language="language",t.system="system",t.am_pm="12",t.twenty_four="24"}(s||(s={}));const i=t=>{if(t.time_format===s.language||t.time_format===s.system){const e=t.time_format===s.language?t.language:void 0,i=(new Date).toLocaleString(e);return i.includes("AM")||i.includes("PM")}return t.time_format===s.am_pm},r=t=>new Intl.DateTimeFormat(t.language,{hour:"numeric",minute:"2-digit",hour12:i(t)}),n=globalThis,o=n.ShadowRoot&&(void 0===n.ShadyCSS||n.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,a=Symbol(),c=new WeakMap;let l=class{constructor(t,e,s){if(this._$cssResult$=!0,s!==a)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o;const e=this.t;if(o&&void 0===t){const s=void 0!==e&&1===e.length;s&&(t=c.get(e)),void 0===t&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),s&&c.set(e,t))}return t}toString(){return this.cssText}};const h=(t,...e)=>{const s=1===t.length?t[0]:e.reduce((e,s,i)=>e+(t=>{if(!0===t._$cssResult$)return t.cssText;if("number"==typeof t)return t;throw Error("Value passed to 'css' function must be a 'css' function result: "+t+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(s)+t[i+1],t[0]);return new l(s,t,a)},d=o?t=>t:t=>t instanceof CSSStyleSheet?(t=>{let e="";for(const s of t.cssRules)e+=s.cssText;return(t=>new l("string"==typeof t?t:t+"",void 0,a))(e)})(t):t,{is:u,defineProperty:p,getOwnPropertyDescriptor:m,getOwnPropertyNames:_,getOwnPropertySymbols:g,getPrototypeOf:y}=Object,f=globalThis,v=f.trustedTypes,$=v?v.emptyScript:"",b=f.reactiveElementPolyfillSupport,w=(t,e)=>t,A={toAttribute(t,e){switch(e){case Boolean:t=t?$:null;break;case Object:case Array:t=null==t?t:JSON.stringify(t)}return t},fromAttribute(t,e){let s=t;switch(e){case Boolean:s=null!==t;break;case Number:s=null===t?null:Number(t);break;case Object:case Array:try{s=JSON.parse(t)}catch(t){s=null}}return s}},x=(t,e)=>!u(t,e),S={attribute:!0,type:String,converter:A,reflect:!1,useDefault:!1,hasChanged:x};Symbol.metadata??=Symbol("metadata"),f.litPropertyMetadata??=new WeakMap;let E=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??=[]).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=S){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){const s=Symbol(),i=this.getPropertyDescriptor(t,s,e);void 0!==i&&p(this.prototype,t,i)}}static getPropertyDescriptor(t,e,s){const{get:i,set:r}=m(this.prototype,t)??{get(){return this[e]},set(t){this[e]=t}};return{get:i,set(e){const n=i?.call(this);r?.call(this,e),this.requestUpdate(t,n,s)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??S}static _$Ei(){if(this.hasOwnProperty(w("elementProperties")))return;const t=y(this);t.finalize(),void 0!==t.l&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty(w("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(w("properties"))){const t=this.properties,e=[..._(t),...g(t)];for(const s of e)this.createProperty(s,t[s])}const t=this[Symbol.metadata];if(null!==t){const e=litPropertyMetadata.get(t);if(void 0!==e)for(const[t,s]of e)this.elementProperties.set(t,s)}this._$Eh=new Map;for(const[t,e]of this.elementProperties){const s=this._$Eu(t,e);void 0!==s&&this._$Eh.set(s,t)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){const e=[];if(Array.isArray(t)){const s=new Set(t.flat(1/0).reverse());for(const t of s)e.unshift(d(t))}else void 0!==t&&e.push(d(t));return e}static _$Eu(t,e){const s=e.attribute;return!1===s?void 0:"string"==typeof s?s:"string"==typeof t?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??=new Set).add(t),void 0!==this.renderRoot&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){const t=new Map,e=this.constructor.elementProperties;for(const s of e.keys())this.hasOwnProperty(s)&&(t.set(s,this[s]),delete this[s]);t.size>0&&(this._$Ep=t)}createRenderRoot(){const t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return((t,e)=>{if(o)t.adoptedStyleSheets=e.map(t=>t instanceof CSSStyleSheet?t:t.styleSheet);else for(const s of e){const e=document.createElement("style"),i=n.litNonce;void 0!==i&&e.setAttribute("nonce",i),e.textContent=s.cssText,t.appendChild(e)}})(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??=this.createRenderRoot(),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,s){this._$AK(t,s)}_$ET(t,e){const s=this.constructor.elementProperties.get(t),i=this.constructor._$Eu(t,s);if(void 0!==i&&!0===s.reflect){const r=(void 0!==s.converter?.toAttribute?s.converter:A).toAttribute(e,s.type);this._$Em=t,null==r?this.removeAttribute(i):this.setAttribute(i,r),this._$Em=null}}_$AK(t,e){const s=this.constructor,i=s._$Eh.get(t);if(void 0!==i&&this._$Em!==i){const t=s.getPropertyOptions(i),r="function"==typeof t.converter?{fromAttribute:t.converter}:void 0!==t.converter?.fromAttribute?t.converter:A;this._$Em=i;const n=r.fromAttribute(e,t.type);this[i]=n??this._$Ej?.get(i)??n,this._$Em=null}}requestUpdate(t,e,s,i=!1,r){if(void 0!==t){const n=this.constructor;if(!1===i&&(r=this[t]),s??=n.getPropertyOptions(t),!((s.hasChanged??x)(r,e)||s.useDefault&&s.reflect&&r===this._$Ej?.get(t)&&!this.hasAttribute(n._$Eu(t,s))))return;this.C(t,e,s)}!1===this.isUpdatePending&&(this._$ES=this._$EP())}C(t,e,{useDefault:s,reflect:i,wrapped:r},n){s&&!(this._$Ej??=new Map).has(t)&&(this._$Ej.set(t,n??e??this[t]),!0!==r||void 0!==n)||(this._$AL.has(t)||(this.hasUpdated||s||(e=void 0),this._$AL.set(t,e)),!0===i&&this._$Em!==t&&(this._$Eq??=new Set).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(t){Promise.reject(t)}const t=this.scheduleUpdate();return null!=t&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??=this.createRenderRoot(),this._$Ep){for(const[t,e]of this._$Ep)this[t]=e;this._$Ep=void 0}const t=this.constructor.elementProperties;if(t.size>0)for(const[e,s]of t){const{wrapped:t}=s,i=this[e];!0!==t||this._$AL.has(e)||void 0===i||this.C(e,void 0,s,i)}}let t=!1;const e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(t=>t.hostUpdate?.()),this.update(e)):this._$EM()}catch(e){throw t=!1,this._$EM(),e}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(t=>t.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&=this._$Eq.forEach(t=>this._$ET(t,this[t])),this._$EM()}updated(t){}firstUpdated(t){}};E.elementStyles=[],E.shadowRootOptions={mode:"open"},E[w("elementProperties")]=new Map,E[w("finalized")]=new Map,b?.({ReactiveElement:E}),(f.reactiveElementVersions??=[]).push("2.1.2");const k=globalThis,C=t=>t,M=k.trustedTypes,P=M?M.createPolicy("lit-html",{createHTML:t=>t}):void 0,T="$lit$",N=`lit$${Math.random().toFixed(9).slice(2)}$`,U="?"+N,O=`<${U}>`,D=document,z=()=>D.createComment(""),R=t=>null===t||"object"!=typeof t&&"function"!=typeof t,H=Array.isArray,I="[ \t\n\f\r]",L=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,j=/-->/g,B=/>/g,V=RegExp(`>|${I}(?:([^\\s"'>=/]+)(${I}*=${I}*(?:[^ \t\n\f\r"'\`<>=]|("|')|))|$)`,"g"),F=/'/g,W=/"/g,q=/^(?:script|style|textarea|title)$/i,Z=t=>(e,...s)=>({_$litType$:t,strings:e,values:s}),G=Z(1),J=Z(2),K=Symbol.for("lit-noChange"),Y=Symbol.for("lit-nothing"),X=new WeakMap,Q=D.createTreeWalker(D,129);function tt(t,e){if(!H(t)||!t.hasOwnProperty("raw"))throw Error("invalid template strings array");return void 0!==P?P.createHTML(e):e}const et=(t,e)=>{const s=t.length-1,i=[];let r,n=2===e?"<svg>":3===e?"<math>":"",o=L;for(let e=0;e<s;e++){const s=t[e];let a,c,l=-1,h=0;for(;h<s.length&&(o.lastIndex=h,c=o.exec(s),null!==c);)h=o.lastIndex,o===L?"!--"===c[1]?o=j:void 0!==c[1]?o=B:void 0!==c[2]?(q.test(c[2])&&(r=RegExp("</"+c[2],"g")),o=V):void 0!==c[3]&&(o=V):o===V?">"===c[0]?(o=r??L,l=-1):void 0===c[1]?l=-2:(l=o.lastIndex-c[2].length,a=c[1],o=void 0===c[3]?V:'"'===c[3]?W:F):o===W||o===F?o=V:o===j||o===B?o=L:(o=V,r=void 0);const d=o===V&&t[e+1].startsWith("/>")?" ":"";n+=o===L?s+O:l>=0?(i.push(a),s.slice(0,l)+T+s.slice(l)+N+d):s+N+(-2===l?e:d)}return[tt(t,n+(t[s]||"<?>")+(2===e?"</svg>":3===e?"</math>":"")),i]};class st{constructor({strings:t,_$litType$:e},s){let i;this.parts=[];let r=0,n=0;const o=t.length-1,a=this.parts,[c,l]=et(t,e);if(this.el=st.createElement(c,s),Q.currentNode=this.el.content,2===e||3===e){const t=this.el.content.firstChild;t.replaceWith(...t.childNodes)}for(;null!==(i=Q.nextNode())&&a.length<o;){if(1===i.nodeType){if(i.hasAttributes())for(const t of i.getAttributeNames())if(t.endsWith(T)){const e=l[n++],s=i.getAttribute(t).split(N),o=/([.?@])?(.*)/.exec(e);a.push({type:1,index:r,name:o[2],strings:s,ctor:"."===o[1]?at:"?"===o[1]?ct:"@"===o[1]?lt:ot}),i.removeAttribute(t)}else t.startsWith(N)&&(a.push({type:6,index:r}),i.removeAttribute(t));if(q.test(i.tagName)){const t=i.textContent.split(N),e=t.length-1;if(e>0){i.textContent=M?M.emptyScript:"";for(let s=0;s<e;s++)i.append(t[s],z()),Q.nextNode(),a.push({type:2,index:++r});i.append(t[e],z())}}}else if(8===i.nodeType)if(i.data===U)a.push({type:2,index:r});else{let t=-1;for(;-1!==(t=i.data.indexOf(N,t+1));)a.push({type:7,index:r}),t+=N.length-1}r++}}static createElement(t,e){const s=D.createElement("template");return s.innerHTML=t,s}}function it(t,e,s=t,i){if(e===K)return e;let r=void 0!==i?s._$Co?.[i]:s._$Cl;const n=R(e)?void 0:e._$litDirective$;return r?.constructor!==n&&(r?._$AO?.(!1),void 0===n?r=void 0:(r=new n(t),r._$AT(t,s,i)),void 0!==i?(s._$Co??=[])[i]=r:s._$Cl=r),void 0!==r&&(e=it(t,r._$AS(t,e.values),r,i)),e}class rt{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){const{el:{content:e},parts:s}=this._$AD,i=(t?.creationScope??D).importNode(e,!0);Q.currentNode=i;let r=Q.nextNode(),n=0,o=0,a=s[0];for(;void 0!==a;){if(n===a.index){let e;2===a.type?e=new nt(r,r.nextSibling,this,t):1===a.type?e=new a.ctor(r,a.name,a.strings,this,t):6===a.type&&(e=new ht(r,this,t)),this._$AV.push(e),a=s[++o]}n!==a?.index&&(r=Q.nextNode(),n++)}return Q.currentNode=D,i}p(t){let e=0;for(const s of this._$AV)void 0!==s&&(void 0!==s.strings?(s._$AI(t,s,e),e+=s.strings.length-2):s._$AI(t[e])),e++}}class nt{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,s,i){this.type=2,this._$AH=Y,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=s,this.options=i,this._$Cv=i?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode;const e=this._$AM;return void 0!==e&&11===t?.nodeType&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=it(this,t,e),R(t)?t===Y||null==t||""===t?(this._$AH!==Y&&this._$AR(),this._$AH=Y):t!==this._$AH&&t!==K&&this._(t):void 0!==t._$litType$?this.$(t):void 0!==t.nodeType?this.T(t):(t=>H(t)||"function"==typeof t?.[Symbol.iterator])(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==Y&&R(this._$AH)?this._$AA.nextSibling.data=t:this.T(D.createTextNode(t)),this._$AH=t}$(t){const{values:e,_$litType$:s}=t,i="number"==typeof s?this._$AC(t):(void 0===s.el&&(s.el=st.createElement(tt(s.h,s.h[0]),this.options)),s);if(this._$AH?._$AD===i)this._$AH.p(e);else{const t=new rt(i,this),s=t.u(this.options);t.p(e),this.T(s),this._$AH=t}}_$AC(t){let e=X.get(t.strings);return void 0===e&&X.set(t.strings,e=new st(t)),e}k(t){H(this._$AH)||(this._$AH=[],this._$AR());const e=this._$AH;let s,i=0;for(const r of t)i===e.length?e.push(s=new nt(this.O(z()),this.O(z()),this,this.options)):s=e[i],s._$AI(r),i++;i<e.length&&(this._$AR(s&&s._$AB.nextSibling,i),e.length=i)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){const e=C(t).nextSibling;C(t).remove(),t=e}}setConnected(t){void 0===this._$AM&&(this._$Cv=t,this._$AP?.(t))}}class ot{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,s,i,r){this.type=1,this._$AH=Y,this._$AN=void 0,this.element=t,this.name=e,this._$AM=i,this.options=r,s.length>2||""!==s[0]||""!==s[1]?(this._$AH=Array(s.length-1).fill(new String),this.strings=s):this._$AH=Y}_$AI(t,e=this,s,i){const r=this.strings;let n=!1;if(void 0===r)t=it(this,t,e,0),n=!R(t)||t!==this._$AH&&t!==K,n&&(this._$AH=t);else{const i=t;let o,a;for(t=r[0],o=0;o<r.length-1;o++)a=it(this,i[s+o],e,o),a===K&&(a=this._$AH[o]),n||=!R(a)||a!==this._$AH[o],a===Y?t=Y:t!==Y&&(t+=(a??"")+r[o+1]),this._$AH[o]=a}n&&!i&&this.j(t)}j(t){t===Y?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}}class at extends ot{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===Y?void 0:t}}class ct extends ot{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==Y)}}class lt extends ot{constructor(t,e,s,i,r){super(t,e,s,i,r),this.type=5}_$AI(t,e=this){if((t=it(this,t,e,0)??Y)===K)return;const s=this._$AH,i=t===Y&&s!==Y||t.capture!==s.capture||t.once!==s.once||t.passive!==s.passive,r=t!==Y&&(s===Y||i);i&&this.element.removeEventListener(this.name,this,s),r&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){"function"==typeof this._$AH?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}}class ht{constructor(t,e,s){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=s}get _$AU(){return this._$AM._$AU}_$AI(t){it(this,t)}}const dt=k.litHtmlPolyfillSupport;dt?.(st,nt),(k.litHtmlVersions??=[]).push("3.3.3");const ut=globalThis;class pt extends E{constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){const t=super.createRenderRoot();return this.renderOptions.renderBefore??=t.firstChild,t}update(t){const e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=((t,e,s)=>{const i=s?.renderBefore??e;let r=i._$litPart$;if(void 0===r){const t=s?.renderBefore??null;i._$litPart$=r=new nt(e.insertBefore(z(),t),t,void 0,s??{})}return r._$AI(t),r})(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return K}}pt._$litElement$=!0,pt.finalized=!0,ut.litElementHydrateSupport?.({LitElement:pt});const mt=ut.litElementPolyfillSupport;mt?.({LitElement:pt}),(ut.litElementVersions??=[]).push("4.2.2");const _t={attribute:!0,type:String,converter:A,reflect:!1,hasChanged:x},gt=(t=_t,e,s)=>{const{kind:i,metadata:r}=s;let n=globalThis.litPropertyMetadata.get(r);if(void 0===n&&globalThis.litPropertyMetadata.set(r,n=new Map),"setter"===i&&((t=Object.create(t)).wrapped=!0),n.set(s.name,t),"accessor"===i){const{name:i}=s;return{set(s){const r=e.get.call(this);e.set.call(this,s),this.requestUpdate(i,r,t,!0,s)},init(e){return void 0!==e&&this.C(i,void 0,t,e),e}}}if("setter"===i){const{name:i}=s;return function(s){const r=this[i];e.call(this,s),this.requestUpdate(i,r,t,!0,s)}}throw Error("Unsupported decorator location: "+i)};function yt(t){return function(t){return(e,s)=>"object"==typeof s?gt(t,e,s):((t,e,s)=>{const i=e.hasOwnProperty(s);return e.constructor.createProperty(s,t),i?Object.getOwnPropertyDescriptor(e,s):void 0})(t,e,s)}({...t,state:!0,attribute:!1})}function ft(t,e=Date.now()){return e+(t??0)}const vt={morning:"Morning",evening:"Evening"},$t={ran:{icon:"mdi:check-circle",label:"Ran",anomaly:!1},reduced:{icon:"mdi:weather-rainy",label:"Reduced by rain",anomaly:!1},waived:{icon:"mdi:hand-back-right",label:"Waived by run-now",anomaly:!1},recovered:{icon:"mdi:backup-restore",label:"Recovered",anomaly:!0},missed:{icon:"mdi:alert-circle",label:"Missed",anomaly:!0},cancelled:{icon:"mdi:cancel",label:"Cancelled",anomaly:!1}},bt={icon:"mdi:circle-outline",label:"No record",anomaly:!1},wt={icon:"mdi:help-circle",label:"Unknown outcome",anomaly:!1},At=/^\d{4}-\d{2}-\d{2}$/;function xt(t){if("string"!=typeof t||!At.test(t))return;const[e,s,i]=t.split("-").map(Number),r=Date.UTC(e,s-1,i);return St(r)===t?r:void 0}function St(t){return new Date(t).toISOString().slice(0,10)}function Et(t,e){return`${t}|${e}`}function kt(t){const e=function(t){const e=xt(t);if(void 0===e)return[];const s=[];for(let t=6;t>=0;t-=1)s.push(St(e-864e5*t));return s}(t.plan.today.irrigation_day),s=new Set(e),i=new Map;let r=t.plan.morning_enabled;const n=Array.isArray(t.history)?t.history:[];for(const t of n)s.has(t.irrigation_day)&&(i.set(Et(t.irrigation_day,t.kind),t),"morning"===t.kind&&(r=!0));return{days:e,kinds:r?["morning","evening"]:["evening"],cells:i}}function Ct(t,e,s){const i=xt(t);return void 0===i?t:function(t,e){try{return new Intl.DateTimeFormat(t,e)}catch{return new Intl.DateTimeFormat(void 0,e)}}(e,{...s,timeZone:"UTC"}).format(new Date(i))}function Mt(t,e,s,i){const r=[Ct(t,i,{weekday:"short",month:"short",day:"numeric"}),vt[e],(void 0===s?bt:$t[s.outcome]??wt).label];return void 0!==s&&(s.effective_s>0&&r.push(`${Math.max(1,Math.round(s.effective_s/60))} min watered`),null!==s.rain_total_mm&&Number.isFinite(s.rain_total_mm)&&r.push(`${function(t){const e={maximumFractionDigits:1};try{return new Intl.NumberFormat(t,e)}catch{return new Intl.NumberFormat(void 0,e)}}(i).format(s.rain_total_mm)} mm rain`)),r.join(" · ")}function Pt(t,e,s){const i=void 0===s?void 0:function(t,e){const s=Date.parse(t);if(!Number.isNaN(s))try{const t=new Intl.DateTimeFormat("en-US",{timeZone:e,year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(new Date(s)),i=e=>t.find(t=>t.type===e)?.value,r=i("year"),n=i("month"),o=i("day");if(void 0===r||void 0===n||void 0===o)return;return`${r}-${n}-${o}`}catch{return}}(t.configured_start,s);return void 0===i?void 0:i===e}function Tt(t){return Number.isNaN(t)?0:Math.min(1,Math.max(0,t))}function Nt(t,e,s){const i=Date.parse(e),r=Date.parse(s)-i;return t.map(t=>{if(!(r>0))return{...t,x0:0,x1:0};const e=Tt((Date.parse(t.start)-i)/r),s=Tt((Date.parse(t.end)-i)/r);return{...t,x0:e,x1:Math.max(e,s)}})}function Ut(t,e){const s=t.zones.map(t=>({zoneId:t.zone_id,name:t.name||(e.get(t.zone_id)??t.zone_id),start:t.planned_start,end:t.planned_end,status:t.status})),i=function(t){let e,s=Number.POSITIVE_INFINITY;for(const i of t){const t=Date.parse(i);t<s&&(s=t,e=i)}return e}(s.map(t=>t.start))??t.scheduled_start,r=function(t){let e,s=Number.NEGATIVE_INFINITY;for(const i of t){const t=Date.parse(i);t>s&&(s=t,e=i)}return e}(s.map(t=>t.end))??t.scheduled_start;return{kind:t.kind,start:i,end:r,source:"run",status:t.status,segments:Nt(s,i,r)}}function Ot(t,e){const s=new Map(t.plan.zones.map(t=>[t.zone_id,t.name])),i=t.plan.today.irrigation_day,{current:r,last:n}=t.runs,o=t.plan.today.cycles,a=(t,r)=>null!==t&&function(t,e,s,i){return t.kind===e.kind&&(Pt(t,s,i)??t.configured_start===e.start)}(t,r,i,e)?Ut(t,s):void 0,c=o.map(t=>a(r,t)??a(n,t)??function(t,e){const s=t.zones.map(t=>({zoneId:t.zone_id,name:e.get(t.zone_id)??t.zone_id,start:t.start,end:t.end,status:"planned"}));return{kind:t.kind,start:t.start,end:t.end,source:"plan",status:"planned",segments:Nt(s,t.start,t.end)}}(t,s)),l=[r,n].find(t=>null!==t&&!o.some(e=>e.kind===t.kind)&&(Pt(t,i,e)??!0));return void 0!==l&&(c.push(Ut(l,s)),c.sort((t,e)=>Date.parse(t.start)-Date.parse(e.start))),{irrigationDay:i,rows:c}}const Dt="not_found",zt="not_loaded";function Rt(t,e){return void 0===e?{type:t}:{type:t,entry_id:e}}const Ht="ha-irrigation-timeline-card",It=5e3,Lt=28,jt=1e3,Bt=72,Vt=28,Ft={completed:"Completed",cancelled:"Cancelled",interrupted:"Interrupted"},Wt=new Set(["planned","pending","running"]);function qt(t,e){return Wt.has(t.status)?function(t,e){const s=Date.parse(t.start),i=Date.parse(t.end)-s;if(i>0&&e>=s&&e<=s+i)return(e-s)/i}(t,e):void 0}class Zt extends pt{constructor(){super(...arguments),this._generation=0,this._pending=!1,this._onDisconnected=()=>{this._unsubscribe=void 0,this._generation+=1},this._onReady=()=>{this._ensureSubscribed()},this._tick=()=>{this._nowMs=ft(this._offsetMs)},this._onVisibilityChange=()=>{"visible"===document.visibilityState&&void 0!==this._view&&(this._tick(),this._refetch())}}get hass(){return this._hass}set hass(t){this._hass=t,this._ensureSubscribed()}get handshake(){return this._handshake}get clockOffsetMs(){return this._offsetMs??0}setConfig(t){const e=t;if(null===e||"object"!=typeof e)throw new Error(`${Ht}: invalid configuration`);const s=e;if(void 0!==s.entry_id&&"string"!=typeof s.entry_id)throw new Error(`${Ht}: invalid configuration (entry_id must be a string)`);const i=this._config;this._config=s,void 0!==i&&i.entry_id!==s.entry_id&&(this._closeSubscription(),this._view=void 0,this._error=void 0,this._syncTicker()),this._ensureSubscribed()}connectedCallback(){super.connectedCallback(),document.addEventListener("visibilitychange",this._onVisibilityChange),void 0!==this._view&&this._tick(),this._syncTicker(),this._ensureSubscribed()}disconnectedCallback(){super.disconnectedCallback(),document.removeEventListener("visibilitychange",this._onVisibilityChange),this._stopTicker(),this._closeSubscription(),this._unlisten()}getCardSize(){const t=this._model();return void 0===t?2:1+t.rows.reduce((t,e)=>t+1+Math.ceil(Math.max(1,e.segments.length)*Lt/50),0)+Math.ceil((72+28*t.history.kinds.length)/50)}getGridOptions(){const t=this._model(),e=56+(t?.rows??[]).reduce((t,e)=>t+40+Math.max(1,e.segments.length)*Lt,0)+(void 0===t?0:72+28*t.history.kinds.length);return{rows:Math.max(2,Math.ceil(e/64)),min_rows:2,columns:12,min_columns:6}}shouldUpdate(t){if(t.has("_view")||t.has("_config")||t.has("_error"))return!0;if(!t.has("_nowMs"))return!1;const e=t.get("_nowMs");return(this._rows()??[]).some(t=>void 0!==this._cursor(t)||void 0!==e&&void 0!==qt(t,e))}render(){return this._config?G`
      <ha-card .header=${this._config.title??"Irrigation"}>
        <div class="content">${this._renderBody()}</div>
      </ha-card>
    `:Y}_model(){const t=this._view;if(void 0===t)return;const e=this._hass?.config?.time_zone;return this._memo?.view===t&&this._memo.timeZone===e||(this._memo={view:t,timeZone:e,rows:Ot(t,e).rows,history:kt(t)}),this._memo}_rows(){return this._model()?.rows}_cursor(t){return void 0===this._nowMs?void 0:qt(t,this._nowMs)}_renderBody(){if(void 0!==this._error)return G`<p class="message error" role="alert">${this._error}</p>`;const t=this._model();return void 0===t?G`<p class="message">Connecting to the irrigation controller…</p>`:G`
      ${0===t.rows.length?G`<p class="message">No cycle is planned today.</p>`:t.rows.map(t=>this._renderRow(t))}
      ${this._renderHistory(t.history)}
    `}_renderHistory(t){const{days:e,kinds:s,cells:i}=t;if(0===e.length)return Y;const r=this._hass?.locale?.language,n=e.length-1;return G`
      <section class="history" aria-labelledby="history-title">
        <header class="history-header" id="history-title">Last 7 days</header>
        <div class="history-grid">
          <span class="history-corner" aria-hidden="true"></span>
          ${e.map((t,e)=>G`
              <span class="history-day" data-day=${t} data-today=${e===n?"true":Y}>
                ${function(t,e){return Ct(t,e,{weekday:"short"})}(t,r)}
              </span>
            `)}
          ${s.map(t=>G`
              <span class="history-kind" data-kind=${t}>${vt[t]}</span>
              ${e.map(e=>this._renderCell(e,t,i.get(Et(e,t)),r))}
            `)}
        </div>
      </section>
    `}_renderCell(t,e,s,i){const r=void 0===s?bt:$t[s.outcome]??wt,n=Mt(t,e,s,i);return G`
      <span
        class="outcome"
        role="img"
        data-day=${t}
        data-kind=${e}
        data-outcome=${s?.outcome??"none"}
        data-anomaly=${r.anomaly?"true":"false"}
        title=${n}
        aria-label=${n}
      >
        <ha-icon icon=${r.icon}></ha-icon>
      </span>
    `}_renderRow(t){const e=Ft[t.status];return G`
      <section class="cycle" data-kind=${t.kind} data-source=${t.source} data-status=${t.status}>
        <header class="cycle-header">
          <span class="cycle-kind">${vt[t.kind]}</span>
          ${void 0===e?Y:G`<span class="cycle-status">${e}</span>`}
          <span class="cycle-window">${this._window(t.start,t.end)}</span>
        </header>
        ${0===t.segments.length?G`<p class="empty">No zones are planned for this cycle.</p>`:this._renderLanes(t)}
      </section>
    `}_renderLanes(t){const{segments:e}=t,s=e.length*Lt,i=this._cursor(t);return G`
      <div class="lanes" style=${`--hic-lanes: ${e.length}`}>
        <ol class="labels">
          ${e.map(t=>G`
              <li class="label" data-status=${t.status}>
                <span class="zone">${t.name}</span>
                <span class="times">${this._window(t.start,t.end)}</span>
              </li>
            `)}
        </ol>
        <svg
          class="bars"
          viewBox="0 0 ${jt} ${s}"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          ${e.map((t,e)=>{const s=t.x0*jt,r=e*Lt+5,n=Math.max(0,(t.x1-t.x0)*jt);return J`
              <rect
                class="segment"
                data-zone=${t.zoneId}
                data-status=${t.status}
                x=${s}
                y=${r}
                width=${n}
                height=${18}
              ></rect>
              ${"running"===t.status?J`
                    <rect
                      class="progress"
                      data-zone=${t.zoneId}
                      x=${s}
                      y=${r}
                      width=${n}
                      height=${18}
                      style=${`transform: scaleX(${function(t,e){switch(t.status){case"running":{const s=t.x1-t.x0;return s>0?(Math.min(t.x1,Math.max(t.x0,e??t.x1))-t.x0)/s:0}case"completed":case"failed":return 1;default:return 0}}(t,i)})`}
                    ></rect>
                  `:Y}
            `})}
          ${void 0===i?Y:J`
              <line
                class="cursor"
                x1=${i*jt}
                x2=${i*jt}
                y1="0"
                y2=${s}
              ></line>
            `}
        </svg>
      </div>
    `}_window(t,e){return`${this._time(t)} – ${this._time(e)}`}_time(t){const e=new Date(t);if(Number.isNaN(e.getTime()))return"–";const s=this._hass?.locale;return s?((t,e)=>r(e).format(t))(e,s):e.toLocaleTimeString()}_ensureSubscribed(){this.isConnected&&void 0!==this._hass&&(this._listen(this._hass.connection),void 0===this._config||void 0!==this._unsubscribe||this._pending||void 0!==this._retry||this._subscribe(this._hass,this._config.entry_id))}async _subscribe(t,e){const s=++this._generation,i=()=>s!==this._generation;this._pending=!0;try{const s=await function(t,e,s){return t.connection.subscribeMessage(e,Rt("ha_irrigation_controller/state_subscribe",s),{resubscribe:!1})}(t,t=>{i()||this._onView(t)},e);i()?s().catch(()=>{}):this._unsubscribe=s}catch(t){i()||(this._error=function(t){switch(t.code){case Dt:return"No irrigation controller was found. Set up the HA Irrigation Controller helper, or pass its entry_id in the card configuration.";case zt:return"The irrigation controller is loading; the card will retry shortly.";default:return`Cannot read the irrigation controller (${t.code}${t.message?`: ${t.message}`:""}); retrying.`}}(function(t){if("object"==typeof t&&null!==t){const e=t;if(void 0!==e.code||void 0!==e.message)return{code:void 0===e.code?"unknown":String(e.code),message:"string"==typeof e.message?e.message:""}}return"number"==typeof t?{code:String(t),message:""}:t instanceof Error?{code:"unknown",message:t.message}:{code:"unknown",message:"string"==typeof t?t:""}}(t)),this._retry=setTimeout(()=>{this._retry=void 0,this._ensureSubscribed()},5e3))}finally{this._pending=!1}this._ensureSubscribed()}_listen(t){this._connection!==t&&(this._unlisten(),this._connection=t,t.addEventListener("disconnected",this._onDisconnected),t.addEventListener("ready",this._onReady))}_unlisten(){const t=this._connection;void 0!==t&&(t.removeEventListener("disconnected",this._onDisconnected),t.removeEventListener("ready",this._onReady),this._connection=void 0)}_onView(t){this._handshake={schema_version:t.schema_version,version:t.version};const e=function(t,e){const s=Date.parse(t);if(!Number.isNaN(s))return s-e}(t.generated_at,Date.now());void 0!==e&&(this._offsetMs=e),this._nowMs=ft(this._offsetMs),this._view=t,void 0!==this._error&&(this._error=void 0),this._syncTicker()}_syncTicker(){const t=this.isConnected&&void 0!==this._view?(e=this._view,"running"===e?.runs.current?.status?1e3:6e4):void 0;var e;t!==this._tickerPeriod&&(this._stopTicker(),void 0!==t&&(this._tickerPeriod=t,this._ticker=setInterval(this._tick,t)))}_stopTicker(){void 0!==this._ticker&&clearInterval(this._ticker),this._ticker=void 0,this._tickerPeriod=void 0}_refetch(){const t=this._hass;if(void 0===t||void 0===this._unsubscribe)return;const e=this._generation;(function(t,e){return t.callWS(Rt("ha_irrigation_controller/state_get",e))})(t,this._config?.entry_id).then(t=>{e===this._generation&&void 0!==this._unsubscribe&&this._onView(t)}).catch(()=>{})}_closeSubscription(){void 0!==this._retry&&(clearTimeout(this._retry),this._retry=void 0),this._generation+=1;const t=this._unsubscribe;this._unsubscribe=void 0,void 0!==t&&t().catch(()=>{})}static get styles(){return h`
      :host {
        display: block;
        --hic-lane-height: ${Lt}px;
      }
      .content {
        padding: 0 16px 16px;
        color: var(--hic-text-color, var(--primary-text-color));
      }
      .message {
        margin: 0;
        padding: 8px 0;
        color: var(--hic-secondary-text-color, var(--secondary-text-color));
      }
      .message.error {
        color: var(--hic-error-color, var(--error-color, #db4437));
      }
      .cycle + .cycle {
        margin-top: 16px;
      }
      .cycle-header {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 6px;
      }
      .cycle-kind {
        font-weight: 500;
      }
      .cycle-status {
        flex: 1;
        font-size: 0.85em;
        color: var(--hic-secondary-text-color, var(--secondary-text-color));
      }
      .cycle[data-status="completed"] .cycle-status {
        color: var(--hic-completed-color, var(--success-color, #43a047));
      }
      .cycle[data-status="cancelled"] .cycle-status,
      .cycle[data-status="interrupted"] .cycle-status {
        color: var(--hic-failed-color, var(--error-color, #db4437));
      }
      .cycle-window,
      .times {
        color: var(--hic-secondary-text-color, var(--secondary-text-color));
        font-variant-numeric: tabular-nums;
      }
      .cycle-window {
        font-size: 0.9em;
      }
      .lanes {
        display: grid;
        grid-template-columns: fit-content(55%) minmax(0, 1fr);
        column-gap: 12px;
        align-items: start;
      }
      .labels {
        list-style: none;
        margin: 0;
        padding: 0;
        min-width: 0;
      }
      .label {
        display: flex;
        gap: 8px;
        height: var(--hic-lane-height);
        line-height: var(--hic-lane-height);
        white-space: nowrap;
        font-size: 0.9em;
      }
      .label .zone {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .label .times {
        flex: none;
      }
      .bars {
        display: block;
        width: 100%;
        height: calc(var(--hic-lanes, 1) * var(--hic-lane-height));
        border-radius: 4px;
        background: var(--hic-track-color, var(--divider-color, rgba(0, 0, 0, 0.12)));
      }
      .segment {
        fill: var(--hic-segment-color, var(--primary-color, #03a9f4));
      }
      /* Inside a run, a zone not yet reached is a promise, not a fact. */
      .segment[data-status="pending"] {
        opacity: 0.4;
      }
      .label[data-status="skipped"] .zone {
        text-decoration: line-through;
        color: var(--hic-secondary-text-color, var(--secondary-text-color));
      }
      /* The running zone's base is a faint track; the overlay is the fill. */
      .segment[data-status="running"] {
        fill: var(--hic-running-color, var(--hic-segment-color, var(--primary-color, #03a9f4)));
        opacity: 0.3;
      }
      .segment[data-status="completed"] {
        fill: var(--hic-completed-color, var(--success-color, #43a047));
      }
      .segment[data-status="failed"] {
        fill: var(--hic-failed-color, var(--error-color, #db4437));
      }
      .segment[data-status="skipped"] {
        fill: var(--hic-skipped-color, var(--disabled-text-color, #bdbdbd));
      }
      .progress {
        fill: var(--hic-running-color, var(--hic-segment-color, var(--primary-color, #03a9f4)));
        transform-box: fill-box;
        transform-origin: left;
        /* Slightly longer than the tick so the fill glides instead of stepping. */
        transition: transform ${1100}ms linear;
      }
      .cursor {
        stroke: var(--hic-cursor-color, var(--primary-text-color, #212121));
        stroke-width: 2px;
        /* preserveAspectRatio="none" would stretch the stroke with the axis. */
        vector-effect: non-scaling-stroke;
      }
      .empty {
        margin: 0;
        font-size: 0.9em;
        color: var(--hic-secondary-text-color, var(--secondary-text-color));
      }

      /* ---------------------------------------------------- history strip */
      /* Like every --hic-* colour above, the history ones are theme hooks:
         never defined here, only read with an HA theme-var fallback. */
      .history {
        margin-top: 16px;
        padding-top: 12px;
        border-top: 1px solid var(--hic-track-color, var(--divider-color, rgba(0, 0, 0, 0.12)));
      }
      .history-header {
        margin-bottom: 6px;
        font-weight: 500;
      }
      .history-grid {
        display: grid;
        grid-template-columns: fit-content(30%) repeat(${7}, minmax(0, 1fr));
        column-gap: 4px;
        row-gap: 2px;
        align-items: center;
      }
      .history-day,
      .history-kind {
        font-size: 0.85em;
        color: var(--hic-secondary-text-color, var(--secondary-text-color));
        white-space: nowrap;
      }
      .history-day {
        text-align: center;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .history-day[data-today] {
        font-weight: 600;
        color: var(--hic-text-color, var(--primary-text-color));
      }
      .history-kind {
        font-size: 0.9em;
        padding-right: 8px;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .outcome {
        display: flex;
        justify-content: center;
        align-items: center;
        height: ${28}px;
        border-radius: 6px;
        color: var(--hic-history-nominal-color, var(--hic-secondary-text-color, var(--secondary-text-color, #727272)));
        --mdc-icon-size: 20px;
      }
      .outcome ha-icon {
        display: flex;
        width: 20px;
        height: 20px;
        color: inherit;
      }
      .outcome[data-outcome="none"] {
        color: var(--hic-history-empty-color, var(--hic-track-color, var(--divider-color, rgba(0, 0, 0, 0.12))));
      }
      .outcome[data-outcome="ran"] {
        color: var(--hic-history-ran-color, var(--hic-completed-color, var(--success-color, #43a047)));
      }
      .outcome[data-outcome="reduced"] {
        color: var(--hic-history-rain-color, var(--info-color, #039be5));
      }
      /* The anomalies are the only filled cells: a badge the eye lands on
         in either theme, with a shape difference for colour-blind viewers. */
      .outcome[data-anomaly="true"] {
        color: var(--hic-history-anomaly-color, var(--hic-error-color, var(--error-color, #db4437)));
        background: var(--hic-history-anomaly-bg, color-mix(in srgb, var(--hic-history-anomaly-color, var(--hic-error-color, var(--error-color, #db4437))) 15%, transparent));
        box-shadow: inset 0 0 0 1px var(--hic-history-anomaly-color, var(--hic-error-color, var(--error-color, #db4437)));
      }
    `}}t([yt()],Zt.prototype,"_config",void 0),t([yt()],Zt.prototype,"_view",void 0),t([yt()],Zt.prototype,"_error",void 0),t([yt()],Zt.prototype,"_nowMs",void 0),customElements.get(Ht)||customElements.define(Ht,Zt),window.customCards=window.customCards??[],window.customCards.some(t=>t.type===Ht)||window.customCards.push({type:Ht,name:"HA Irrigation Timeline Card",description:"Today's irrigation plan: each cycle's zones as a proportional timeline with planned times, live progress and outcomes, plus the last seven days at a glance."}),console.info(`%c ${Ht.toUpperCase()} %c v0.1.0`,"color: white; background: #2e7d32; font-weight: 700;","");export{Bt as HISTORY_HEADER_PX,Vt as HISTORY_ROW_PX,Zt as HaIrrigationTimelineCard,Lt as LANE_HEIGHT,It as RETRY_DELAY_MS};

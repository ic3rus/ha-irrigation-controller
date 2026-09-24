function t(t,e,i,s){var r,o=arguments.length,n=o<3?e:null===s?s=Object.getOwnPropertyDescriptor(e,i):s;if("object"==typeof Reflect&&"function"==typeof Reflect.decorate)n=Reflect.decorate(t,e,i,s);else for(var a=t.length-1;a>=0;a--)(r=t[a])&&(n=(o<3?r(n):o>3?r(e,i,n):r(e,i))||n);return o>3&&n&&Object.defineProperty(e,i,n),n}var e,i;"function"==typeof SuppressedError&&SuppressedError,function(t){t.language="language",t.system="system",t.comma_decimal="comma_decimal",t.decimal_comma="decimal_comma",t.space_comma="space_comma",t.none="none"}(e||(e={})),function(t){t.language="language",t.system="system",t.am_pm="12",t.twenty_four="24"}(i||(i={}));const s=t=>{if(t.time_format===i.language||t.time_format===i.system){const e=t.time_format===i.language?t.language:void 0,s=(new Date).toLocaleString(e);return s.includes("AM")||s.includes("PM")}return t.time_format===i.am_pm},r=t=>new Intl.DateTimeFormat(t.language,{hour:"numeric",minute:"2-digit",hour12:s(t)}),o=globalThis,n=o.ShadowRoot&&(void 0===o.ShadyCSS||o.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,a=Symbol(),c=new WeakMap;let l=class{constructor(t,e,i){if(this._$cssResult$=!0,i!==a)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o;const e=this.t;if(n&&void 0===t){const i=void 0!==e&&1===e.length;i&&(t=c.get(e)),void 0===t&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),i&&c.set(e,t))}return t}toString(){return this.cssText}};const h=(t,...e)=>{const i=1===t.length?t[0]:e.reduce((e,i,s)=>e+(t=>{if(!0===t._$cssResult$)return t.cssText;if("number"==typeof t)return t;throw Error("Value passed to 'css' function must be a 'css' function result: "+t+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(i)+t[s+1],t[0]);return new l(i,t,a)},d=n?t=>t:t=>t instanceof CSSStyleSheet?(t=>{let e="";for(const i of t.cssRules)e+=i.cssText;return(t=>new l("string"==typeof t?t:t+"",void 0,a))(e)})(t):t,{is:u,defineProperty:p,getOwnPropertyDescriptor:m,getOwnPropertyNames:_,getOwnPropertySymbols:y,getPrototypeOf:f}=Object,g=globalThis,v=g.trustedTypes,$=v?v.emptyScript:"",b=g.reactiveElementPolyfillSupport,w=(t,e)=>t,x={toAttribute(t,e){switch(e){case Boolean:t=t?$:null;break;case Object:case Array:t=null==t?t:JSON.stringify(t)}return t},fromAttribute(t,e){let i=t;switch(e){case Boolean:i=null!==t;break;case Number:i=null===t?null:Number(t);break;case Object:case Array:try{i=JSON.parse(t)}catch(t){i=null}}return i}},A=(t,e)=>!u(t,e),k={attribute:!0,type:String,converter:x,reflect:!1,useDefault:!1,hasChanged:A};Symbol.metadata??=Symbol("metadata"),g.litPropertyMetadata??=new WeakMap;let S=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??=[]).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=k){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){const i=Symbol(),s=this.getPropertyDescriptor(t,i,e);void 0!==s&&p(this.prototype,t,s)}}static getPropertyDescriptor(t,e,i){const{get:s,set:r}=m(this.prototype,t)??{get(){return this[e]},set(t){this[e]=t}};return{get:s,set(e){const o=s?.call(this);r?.call(this,e),this.requestUpdate(t,o,i)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??k}static _$Ei(){if(this.hasOwnProperty(w("elementProperties")))return;const t=f(this);t.finalize(),void 0!==t.l&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty(w("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(w("properties"))){const t=this.properties,e=[..._(t),...y(t)];for(const i of e)this.createProperty(i,t[i])}const t=this[Symbol.metadata];if(null!==t){const e=litPropertyMetadata.get(t);if(void 0!==e)for(const[t,i]of e)this.elementProperties.set(t,i)}this._$Eh=new Map;for(const[t,e]of this.elementProperties){const i=this._$Eu(t,e);void 0!==i&&this._$Eh.set(i,t)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){const e=[];if(Array.isArray(t)){const i=new Set(t.flat(1/0).reverse());for(const t of i)e.unshift(d(t))}else void 0!==t&&e.push(d(t));return e}static _$Eu(t,e){const i=e.attribute;return!1===i?void 0:"string"==typeof i?i:"string"==typeof t?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??=new Set).add(t),void 0!==this.renderRoot&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){const t=new Map,e=this.constructor.elementProperties;for(const i of e.keys())this.hasOwnProperty(i)&&(t.set(i,this[i]),delete this[i]);t.size>0&&(this._$Ep=t)}createRenderRoot(){const t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return((t,e)=>{if(n)t.adoptedStyleSheets=e.map(t=>t instanceof CSSStyleSheet?t:t.styleSheet);else for(const i of e){const e=document.createElement("style"),s=o.litNonce;void 0!==s&&e.setAttribute("nonce",s),e.textContent=i.cssText,t.appendChild(e)}})(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??=this.createRenderRoot(),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,i){this._$AK(t,i)}_$ET(t,e){const i=this.constructor.elementProperties.get(t),s=this.constructor._$Eu(t,i);if(void 0!==s&&!0===i.reflect){const r=(void 0!==i.converter?.toAttribute?i.converter:x).toAttribute(e,i.type);this._$Em=t,null==r?this.removeAttribute(s):this.setAttribute(s,r),this._$Em=null}}_$AK(t,e){const i=this.constructor,s=i._$Eh.get(t);if(void 0!==s&&this._$Em!==s){const t=i.getPropertyOptions(s),r="function"==typeof t.converter?{fromAttribute:t.converter}:void 0!==t.converter?.fromAttribute?t.converter:x;this._$Em=s;const o=r.fromAttribute(e,t.type);this[s]=o??this._$Ej?.get(s)??o,this._$Em=null}}requestUpdate(t,e,i,s=!1,r){if(void 0!==t){const o=this.constructor;if(!1===s&&(r=this[t]),i??=o.getPropertyOptions(t),!((i.hasChanged??A)(r,e)||i.useDefault&&i.reflect&&r===this._$Ej?.get(t)&&!this.hasAttribute(o._$Eu(t,i))))return;this.C(t,e,i)}!1===this.isUpdatePending&&(this._$ES=this._$EP())}C(t,e,{useDefault:i,reflect:s,wrapped:r},o){i&&!(this._$Ej??=new Map).has(t)&&(this._$Ej.set(t,o??e??this[t]),!0!==r||void 0!==o)||(this._$AL.has(t)||(this.hasUpdated||i||(e=void 0),this._$AL.set(t,e)),!0===s&&this._$Em!==t&&(this._$Eq??=new Set).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(t){Promise.reject(t)}const t=this.scheduleUpdate();return null!=t&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??=this.createRenderRoot(),this._$Ep){for(const[t,e]of this._$Ep)this[t]=e;this._$Ep=void 0}const t=this.constructor.elementProperties;if(t.size>0)for(const[e,i]of t){const{wrapped:t}=i,s=this[e];!0!==t||this._$AL.has(e)||void 0===s||this.C(e,void 0,i,s)}}let t=!1;const e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(t=>t.hostUpdate?.()),this.update(e)):this._$EM()}catch(e){throw t=!1,this._$EM(),e}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(t=>t.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&=this._$Eq.forEach(t=>this._$ET(t,this[t])),this._$EM()}updated(t){}firstUpdated(t){}};S.elementStyles=[],S.shadowRootOptions={mode:"open"},S[w("elementProperties")]=new Map,S[w("finalized")]=new Map,b?.({ReactiveElement:S}),(g.reactiveElementVersions??=[]).push("2.1.2");const E=globalThis,C=t=>t,M=E.trustedTypes,T=M?M.createPolicy("lit-html",{createHTML:t=>t}):void 0,P="$lit$",N=`lit$${Math.random().toFixed(9).slice(2)}$`,z="?"+N,U=`<${z}>`,O=document,D=()=>O.createComment(""),R=t=>null===t||"object"!=typeof t&&"function"!=typeof t,H=Array.isArray,I="[ \t\n\f\r]",j=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,L=/-->/g,V=/>/g,B=RegExp(`>|${I}(?:([^\\s"'>=/]+)(${I}*=${I}*(?:[^ \t\n\f\r"'\`<>=]|("|')|))|$)`,"g"),F=/'/g,W=/"/g,q=/^(?:script|style|textarea|title)$/i,Z=t=>(e,...i)=>({_$litType$:t,strings:e,values:i}),G=Z(1),J=Z(2),K=Symbol.for("lit-noChange"),Y=Symbol.for("lit-nothing"),X=new WeakMap,Q=O.createTreeWalker(O,129);function tt(t,e){if(!H(t)||!t.hasOwnProperty("raw"))throw Error("invalid template strings array");return void 0!==T?T.createHTML(e):e}const et=(t,e)=>{const i=t.length-1,s=[];let r,o=2===e?"<svg>":3===e?"<math>":"",n=j;for(let e=0;e<i;e++){const i=t[e];let a,c,l=-1,h=0;for(;h<i.length&&(n.lastIndex=h,c=n.exec(i),null!==c);)h=n.lastIndex,n===j?"!--"===c[1]?n=L:void 0!==c[1]?n=V:void 0!==c[2]?(q.test(c[2])&&(r=RegExp("</"+c[2],"g")),n=B):void 0!==c[3]&&(n=B):n===B?">"===c[0]?(n=r??j,l=-1):void 0===c[1]?l=-2:(l=n.lastIndex-c[2].length,a=c[1],n=void 0===c[3]?B:'"'===c[3]?W:F):n===W||n===F?n=B:n===L||n===V?n=j:(n=B,r=void 0);const d=n===B&&t[e+1].startsWith("/>")?" ":"";o+=n===j?i+U:l>=0?(s.push(a),i.slice(0,l)+P+i.slice(l)+N+d):i+N+(-2===l?e:d)}return[tt(t,o+(t[i]||"<?>")+(2===e?"</svg>":3===e?"</math>":"")),s]};class it{constructor({strings:t,_$litType$:e},i){let s;this.parts=[];let r=0,o=0;const n=t.length-1,a=this.parts,[c,l]=et(t,e);if(this.el=it.createElement(c,i),Q.currentNode=this.el.content,2===e||3===e){const t=this.el.content.firstChild;t.replaceWith(...t.childNodes)}for(;null!==(s=Q.nextNode())&&a.length<n;){if(1===s.nodeType){if(s.hasAttributes())for(const t of s.getAttributeNames())if(t.endsWith(P)){const e=l[o++],i=s.getAttribute(t).split(N),n=/([.?@])?(.*)/.exec(e);a.push({type:1,index:r,name:n[2],strings:i,ctor:"."===n[1]?at:"?"===n[1]?ct:"@"===n[1]?lt:nt}),s.removeAttribute(t)}else t.startsWith(N)&&(a.push({type:6,index:r}),s.removeAttribute(t));if(q.test(s.tagName)){const t=s.textContent.split(N),e=t.length-1;if(e>0){s.textContent=M?M.emptyScript:"";for(let i=0;i<e;i++)s.append(t[i],D()),Q.nextNode(),a.push({type:2,index:++r});s.append(t[e],D())}}}else if(8===s.nodeType)if(s.data===z)a.push({type:2,index:r});else{let t=-1;for(;-1!==(t=s.data.indexOf(N,t+1));)a.push({type:7,index:r}),t+=N.length-1}r++}}static createElement(t,e){const i=O.createElement("template");return i.innerHTML=t,i}}function st(t,e,i=t,s){if(e===K)return e;let r=void 0!==s?i._$Co?.[s]:i._$Cl;const o=R(e)?void 0:e._$litDirective$;return r?.constructor!==o&&(r?._$AO?.(!1),void 0===o?r=void 0:(r=new o(t),r._$AT(t,i,s)),void 0!==s?(i._$Co??=[])[s]=r:i._$Cl=r),void 0!==r&&(e=st(t,r._$AS(t,e.values),r,s)),e}class rt{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){const{el:{content:e},parts:i}=this._$AD,s=(t?.creationScope??O).importNode(e,!0);Q.currentNode=s;let r=Q.nextNode(),o=0,n=0,a=i[0];for(;void 0!==a;){if(o===a.index){let e;2===a.type?e=new ot(r,r.nextSibling,this,t):1===a.type?e=new a.ctor(r,a.name,a.strings,this,t):6===a.type&&(e=new ht(r,this,t)),this._$AV.push(e),a=i[++n]}o!==a?.index&&(r=Q.nextNode(),o++)}return Q.currentNode=O,s}p(t){let e=0;for(const i of this._$AV)void 0!==i&&(void 0!==i.strings?(i._$AI(t,i,e),e+=i.strings.length-2):i._$AI(t[e])),e++}}class ot{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,i,s){this.type=2,this._$AH=Y,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=i,this.options=s,this._$Cv=s?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode;const e=this._$AM;return void 0!==e&&11===t?.nodeType&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=st(this,t,e),R(t)?t===Y||null==t||""===t?(this._$AH!==Y&&this._$AR(),this._$AH=Y):t!==this._$AH&&t!==K&&this._(t):void 0!==t._$litType$?this.$(t):void 0!==t.nodeType?this.T(t):(t=>H(t)||"function"==typeof t?.[Symbol.iterator])(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==Y&&R(this._$AH)?this._$AA.nextSibling.data=t:this.T(O.createTextNode(t)),this._$AH=t}$(t){const{values:e,_$litType$:i}=t,s="number"==typeof i?this._$AC(t):(void 0===i.el&&(i.el=it.createElement(tt(i.h,i.h[0]),this.options)),i);if(this._$AH?._$AD===s)this._$AH.p(e);else{const t=new rt(s,this),i=t.u(this.options);t.p(e),this.T(i),this._$AH=t}}_$AC(t){let e=X.get(t.strings);return void 0===e&&X.set(t.strings,e=new it(t)),e}k(t){H(this._$AH)||(this._$AH=[],this._$AR());const e=this._$AH;let i,s=0;for(const r of t)s===e.length?e.push(i=new ot(this.O(D()),this.O(D()),this,this.options)):i=e[s],i._$AI(r),s++;s<e.length&&(this._$AR(i&&i._$AB.nextSibling,s),e.length=s)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){const e=C(t).nextSibling;C(t).remove(),t=e}}setConnected(t){void 0===this._$AM&&(this._$Cv=t,this._$AP?.(t))}}class nt{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,i,s,r){this.type=1,this._$AH=Y,this._$AN=void 0,this.element=t,this.name=e,this._$AM=s,this.options=r,i.length>2||""!==i[0]||""!==i[1]?(this._$AH=Array(i.length-1).fill(new String),this.strings=i):this._$AH=Y}_$AI(t,e=this,i,s){const r=this.strings;let o=!1;if(void 0===r)t=st(this,t,e,0),o=!R(t)||t!==this._$AH&&t!==K,o&&(this._$AH=t);else{const s=t;let n,a;for(t=r[0],n=0;n<r.length-1;n++)a=st(this,s[i+n],e,n),a===K&&(a=this._$AH[n]),o||=!R(a)||a!==this._$AH[n],a===Y?t=Y:t!==Y&&(t+=(a??"")+r[n+1]),this._$AH[n]=a}o&&!s&&this.j(t)}j(t){t===Y?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}}class at extends nt{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===Y?void 0:t}}class ct extends nt{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==Y)}}class lt extends nt{constructor(t,e,i,s,r){super(t,e,i,s,r),this.type=5}_$AI(t,e=this){if((t=st(this,t,e,0)??Y)===K)return;const i=this._$AH,s=t===Y&&i!==Y||t.capture!==i.capture||t.once!==i.once||t.passive!==i.passive,r=t!==Y&&(i===Y||s);s&&this.element.removeEventListener(this.name,this,i),r&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){"function"==typeof this._$AH?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}}class ht{constructor(t,e,i){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=i}get _$AU(){return this._$AM._$AU}_$AI(t){st(this,t)}}const dt=E.litHtmlPolyfillSupport;dt?.(it,ot),(E.litHtmlVersions??=[]).push("3.3.3");const ut=globalThis;class pt extends S{constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){const t=super.createRenderRoot();return this.renderOptions.renderBefore??=t.firstChild,t}update(t){const e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=((t,e,i)=>{const s=i?.renderBefore??e;let r=s._$litPart$;if(void 0===r){const t=i?.renderBefore??null;s._$litPart$=r=new ot(e.insertBefore(D(),t),t,void 0,i??{})}return r._$AI(t),r})(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return K}}pt._$litElement$=!0,pt.finalized=!0,ut.litElementHydrateSupport?.({LitElement:pt});const mt=ut.litElementPolyfillSupport;mt?.({LitElement:pt}),(ut.litElementVersions??=[]).push("4.2.2");const _t={attribute:!0,type:String,converter:x,reflect:!1,hasChanged:A},yt=(t=_t,e,i)=>{const{kind:s,metadata:r}=i;let o=globalThis.litPropertyMetadata.get(r);if(void 0===o&&globalThis.litPropertyMetadata.set(r,o=new Map),"setter"===s&&((t=Object.create(t)).wrapped=!0),o.set(i.name,t),"accessor"===s){const{name:s}=i;return{set(i){const r=e.get.call(this);e.set.call(this,i),this.requestUpdate(s,r,t,!0,i)},init(e){return void 0!==e&&this.C(s,void 0,t,e),e}}}if("setter"===s){const{name:s}=i;return function(i){const r=this[s];e.call(this,i),this.requestUpdate(s,r,t,!0,i)}}throw Error("Unsupported decorator location: "+s)};function ft(t){return function(t){return(e,i)=>"object"==typeof i?yt(t,e,i):((t,e,i)=>{const s=e.hasOwnProperty(i);return e.constructor.createProperty(i,t),s?Object.getOwnPropertyDescriptor(e,i):void 0})(t,e,i)}({...t,state:!0,attribute:!1})}function gt(t,e=Date.now()){return e+(t??0)}const vt={pump_on_unconfirmed:{icon:"mdi:water-pump-off",label:"Pump did not confirm turning on"},pump_off_unconfirmed:{icon:"mdi:water-pump",label:"Pump did not confirm turning off"},valve_open_unconfirmed:{icon:"mdi:valve-closed",label:"Valve did not confirm opening"},valve_close_unconfirmed:{icon:"mdi:valve-open",label:"Valve did not confirm closing"},journal_save_failed:{icon:"mdi:content-save-alert",label:"Cycle journal could not be saved"},configured_entity_missing:{icon:"mdi:link-off",label:"Configured entity is missing"},cycle_interrupted:{icon:"mdi:power-plug-off",label:"A cycle was interrupted"},cycle_recovered:{icon:"mdi:backup-restore",label:"A cycle was recovered at startup"},missed_cycle:{icon:"mdi:alert-circle",label:"A scheduled cycle did not run"},manual_valve_timeout:{icon:"mdi:timer-off",label:"A switch left open by hand was closed"}},$t={icon:"mdi:help-circle",label:"Unknown anomaly"},bt="mdi:check-circle-outline",wt="All is well",xt="mdi:alert";function At(t){return"string"==typeof t&&""!==t?t:void 0}function kt(t,e){const i=At(t.zone_id);if(void 0!==i){const t=Array.isArray(e.plan?.zones)?e.plan.zones:[];return t.find(t=>t.zone_id===i)?.name??i}return At(t.entity_id)??At(t.role)??null}function St(t){const e=Array.isArray(t.health?.open)?t.health.open:[],i=[];for(const s of e){if(null===s||"object"!=typeof s)continue;const e=s,r=At(e.anomaly);void 0!==r&&i.push({kind:r,visual:Object.hasOwn(vt,r)?vt[r]:$t,subject:kt(e,t)})}return{state:0===i.length?"nominal":"anomaly",items:i}}function Et(t){return t<=0?wt:1===t?"1 issue needs attention":`${t} issues need attention`}const Ct={morning:"Morning",evening:"Evening"},Mt={ran:{icon:"mdi:check-circle",label:"Ran",anomaly:!1},reduced:{icon:"mdi:weather-rainy",label:"Reduced by rain",anomaly:!1},waived:{icon:"mdi:hand-back-right",label:"Waived by run-now",anomaly:!1},recovered:{icon:"mdi:backup-restore",label:"Recovered",anomaly:!0},missed:{icon:"mdi:alert-circle",label:"Missed",anomaly:!0},cancelled:{icon:"mdi:cancel",label:"Cancelled",anomaly:!1}},Tt={icon:"mdi:circle-outline",label:"No record",anomaly:!1},Pt={icon:"mdi:help-circle",label:"Unknown outcome",anomaly:!1},Nt=/^\d{4}-\d{2}-\d{2}$/;function zt(t){if("string"!=typeof t||!Nt.test(t))return;const[e,i,s]=t.split("-").map(Number),r=Date.UTC(e,i-1,s);return Ut(r)===t?r:void 0}function Ut(t){return new Date(t).toISOString().slice(0,10)}function Ot(t,e){return`${t}|${e}`}function Dt(t){const e=function(t){const e=zt(t);if(void 0===e)return[];const i=[];for(let t=6;t>=0;t-=1)i.push(Ut(e-864e5*t));return i}(t.plan.today.irrigation_day),i=new Set(e),s=new Map;let r=t.plan.morning_enabled;const o=Array.isArray(t.history)?t.history:[];for(const t of o)i.has(t.irrigation_day)&&(s.set(Ot(t.irrigation_day,t.kind),t),"morning"===t.kind&&(r=!0));return{days:e,kinds:r?["morning","evening"]:["evening"],cells:s}}function Rt(t,e,i){const s=zt(t);return void 0===s?t:function(t,e){try{return new Intl.DateTimeFormat(t,e)}catch{return new Intl.DateTimeFormat(void 0,e)}}(e,{...i,timeZone:"UTC"}).format(new Date(s))}function Ht(t,e,i,s){const r=[Rt(t,s,{weekday:"short",month:"short",day:"numeric"}),Ct[e],(void 0===i?Tt:Mt[i.outcome]??Pt).label];return void 0!==i&&(i.effective_s>0&&r.push(`${Math.max(1,Math.round(i.effective_s/60))} min watered`),null!==i.rain_total_mm&&Number.isFinite(i.rain_total_mm)&&r.push(`${function(t){const e={maximumFractionDigits:1};try{return new Intl.NumberFormat(t,e)}catch{return new Intl.NumberFormat(void 0,e)}}(s).format(i.rain_total_mm)} mm rain`)),r.join(" · ")}function It(t,e,i){const s=void 0===i?void 0:function(t,e){const i=Date.parse(t);if(!Number.isNaN(i))try{const t=new Intl.DateTimeFormat("en-US",{timeZone:e,year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(new Date(i)),s=e=>t.find(t=>t.type===e)?.value,r=s("year"),o=s("month"),n=s("day");if(void 0===r||void 0===o||void 0===n)return;return`${r}-${o}-${n}`}catch{return}}(t.configured_start,i);return void 0===s?void 0:s===e}function jt(t){return Number.isNaN(t)?0:Math.min(1,Math.max(0,t))}function Lt(t,e,i){const s=Date.parse(e),r=Date.parse(i)-s;return t.map(t=>{if(!(r>0))return{...t,x0:0,x1:0};const e=jt((Date.parse(t.start)-s)/r),i=jt((Date.parse(t.end)-s)/r);return{...t,x0:e,x1:Math.max(e,i)}})}function Vt(t,e){const i=t.zones.map(t=>({zoneId:t.zone_id,name:t.name||(e.get(t.zone_id)??t.zone_id),start:t.planned_start,end:t.planned_end,status:t.status})),s=function(t){let e,i=Number.POSITIVE_INFINITY;for(const s of t){const t=Date.parse(s);t<i&&(i=t,e=s)}return e}(i.map(t=>t.start))??t.scheduled_start,r=function(t){let e,i=Number.NEGATIVE_INFINITY;for(const s of t){const t=Date.parse(s);t>i&&(i=t,e=s)}return e}(i.map(t=>t.end))??t.scheduled_start;return{kind:t.kind,start:s,end:r,source:"run",status:t.status,segments:Lt(i,s,r)}}function Bt(t,e){const i=new Map(t.plan.zones.map(t=>[t.zone_id,t.name])),s=t.plan.today.irrigation_day,{current:r,last:o}=t.runs,n=t.plan.today.cycles,a=(t,r)=>null!==t&&function(t,e,i,s){return t.kind===e.kind&&(It(t,i,s)??t.configured_start===e.start)}(t,r,s,e)?Vt(t,i):void 0,c=n.map(t=>a(r,t)??a(o,t)??function(t,e){const i=t.zones.map(t=>({zoneId:t.zone_id,name:e.get(t.zone_id)??t.zone_id,start:t.start,end:t.end,status:"planned"}));return{kind:t.kind,start:t.start,end:t.end,source:"plan",status:"planned",segments:Lt(i,t.start,t.end)}}(t,i)),l=[r,o].find(t=>null!==t&&!n.some(e=>e.kind===t.kind)&&(It(t,s,e)??!0));return void 0!==l&&(c.push(Vt(l,i)),c.sort((t,e)=>Date.parse(t.start)-Date.parse(e.start))),{irrigationDay:s,rows:c}}const Ft="not_found",Wt="not_loaded";function qt(t,e){return void 0===e?{type:t}:{type:t,entry_id:e}}const Zt="ha-irrigation-timeline-card",Gt=5e3,Jt=28,Kt=1e3,Yt=72,Xt=28,Qt=68,te=24,ee={completed:"Completed",cancelled:"Cancelled",interrupted:"Interrupted"},ie={failed:"mdi:alert-circle",completed:"mdi:check"},se={failed:"failed",completed:"completed",skipped:"skipped"},re=new Set(["planned","pending","running"]);function oe(t,e){return re.has(t.status)?function(t,e){const i=Date.parse(t.start),s=Date.parse(t.end)-i;if(s>0&&e>=i&&e<=i+s)return(e-i)/s}(t,e):void 0}class ne extends pt{constructor(){super(...arguments),this._generation=0,this._pending=!1,this._onDisconnected=()=>{this._unsubscribe=void 0,this._generation+=1},this._onReady=()=>{this._ensureSubscribed()},this._tick=()=>{this._nowMs=gt(this._offsetMs)},this._onVisibilityChange=()=>{"visible"===document.visibilityState&&void 0!==this._view&&(this._tick(),this._refetch())}}get hass(){return this._hass}set hass(t){this._hass=t,this._ensureSubscribed()}get handshake(){return this._handshake}get clockOffsetMs(){return this._offsetMs??0}setConfig(t){const e=t;if(null===e||"object"!=typeof e)throw new Error(`${Zt}: invalid configuration`);const i=e;if(void 0!==i.entry_id&&"string"!=typeof i.entry_id)throw new Error(`${Zt}: invalid configuration (entry_id must be a string)`);const s=this._config;this._config=i,void 0!==s&&s.entry_id!==i.entry_id&&(this._closeSubscription(),this._view=void 0,this._error=void 0,this._syncTicker()),this._ensureSubscribed()}connectedCallback(){super.connectedCallback(),document.addEventListener("visibilitychange",this._onVisibilityChange),void 0!==this._view&&this._tick(),this._syncTicker(),this._ensureSubscribed()}disconnectedCallback(){super.disconnectedCallback(),document.removeEventListener("visibilitychange",this._onVisibilityChange),this._stopTicker(),this._closeSubscription(),this._unlisten()}getCardSize(){const t=this._model();return void 0===t?2:1+Math.ceil(ae(t.health)/50)+t.rows.reduce((t,e)=>t+1+Math.ceil(Math.max(1,e.segments.length)*Jt/50),0)+Math.ceil((72+28*t.history.kinds.length)/50)}getGridOptions(){const t=this._model(),e=56+(t?.rows??[]).reduce((t,e)=>t+40+Math.max(1,e.segments.length)*Jt,0)+(void 0===t?0:ae(t.health)+72+28*t.history.kinds.length);return{rows:Math.max(2,Math.ceil(e/64)),min_rows:2,columns:12,min_columns:6}}shouldUpdate(t){if(t.has("_view")||t.has("_config")||t.has("_error"))return!0;if(!t.has("_nowMs"))return!1;const e=t.get("_nowMs");return(this._rows()??[]).some(t=>void 0!==this._cursor(t)||void 0!==e&&void 0!==oe(t,e))}render(){if(!this._config)return Y;const t=void 0===this._error?this._model():void 0;return G`
      <ha-card>
        <div class="card-header">
          <h1 class="title">${this._config.title??"Irrigation"}</h1>
          ${void 0===t?Y:this._renderChip(t.health)}
          <span class="sr-only health-announcer" role="status" aria-live="polite"
            >${void 0===t?"":Et(t.health.items.length)}</span
          >
        </div>
        <div class="content">${this._renderBody()}</div>
      </ha-card>
    `}_model(){const t=this._view;if(void 0===t)return;const e=this._hass?.config?.time_zone;return this._memo?.view===t&&this._memo.timeZone===e||(this._memo={view:t,timeZone:e,rows:Bt(t,e).rows,history:Dt(t),health:St(t)}),this._memo}_rows(){return this._model()?.rows}_cursor(t){return void 0===this._nowMs?void 0:oe(t,this._nowMs)}_renderBody(){if(void 0!==this._error)return G`<p class="message error" role="alert">${this._error}</p>`;const t=this._model();return void 0===t?G`<p class="message">Connecting to the irrigation controller…</p>`:G`
      ${this._renderAnomalies(t.health)}
      ${0===t.rows.length?G`<p class="message">No cycle is planned today.</p>`:t.rows.map(t=>this._renderRow(t))}
      ${this._renderHistory(t.history)}
    `}_renderChip(t){const e=t.items.length,i="nominal"===t.state?bt:xt;return G`
      <span class="health" data-state=${t.state} role="img" aria-label=${Et(e)}>
        <ha-icon icon=${i}></ha-icon>
        <span class="health-text">${function(t){return t<=0?wt:1===t?"1 issue":`${t} issues`}(e)}</span>
      </span>
    `}_renderAnomalies(t){return"nominal"===t.state?Y:G`
      <section class="anomalies" role="status" aria-live="polite">
        <header class="anomalies-header">
          <ha-icon icon=${xt}></ha-icon>
          <span>${Et(t.items.length)}</span>
        </header>
        <ul class="anomaly-list">
          ${t.items.map(t=>G`
              <li class="anomaly" data-anomaly=${t.kind}>
                <ha-icon icon=${t.visual.icon}></ha-icon>
                <span class="anomaly-text"
                  >${function(t){return t.visual===$t?`${$t.label} (${t.kind})`:t.visual.label}(t)}${null===t.subject?Y:G` · <span class="anomaly-subject">${t.subject}</span>`}</span
                >
              </li>
            `)}
        </ul>
      </section>
    `}_statusGlyph(t){const e=ie[t];return void 0===e?Y:G`<ha-icon class="status-glyph" icon=${e}></ha-icon>`}_statusText(t){const e=se[t];return void 0===e?Y:G`<span class="sr-only">${e}</span>`}_renderHistory(t){const{days:e,kinds:i,cells:s}=t;if(0===e.length)return Y;const r=this._hass?.locale?.language,o=e.length-1;return G`
      <section class="history" aria-labelledby="history-title">
        <header class="history-header" id="history-title">Last 7 days</header>
        <div class="history-grid">
          <span class="history-corner" aria-hidden="true"></span>
          ${e.map((t,e)=>G`
              <span class="history-day" data-day=${t} data-today=${e===o?"true":Y}>
                ${function(t,e){return Rt(t,e,{weekday:"short"})}(t,r)}
              </span>
            `)}
          ${i.map(t=>G`
              <span class="history-kind" data-kind=${t}>${Ct[t]}</span>
              ${e.map(e=>this._renderCell(e,t,s.get(Ot(e,t)),r))}
            `)}
        </div>
      </section>
    `}_renderCell(t,e,i,s){const r=void 0===i?Tt:Mt[i.outcome]??Pt,o=Ht(t,e,i,s);return G`
      <span
        class="outcome"
        role="img"
        data-day=${t}
        data-kind=${e}
        data-outcome=${i?.outcome??"none"}
        data-anomaly=${r.anomaly?"true":"false"}
        title=${o}
        aria-label=${o}
      >
        <ha-icon icon=${r.icon}></ha-icon>
      </span>
    `}_renderRow(t){const e=ee[t.status];return G`
      <section class="cycle" data-kind=${t.kind} data-source=${t.source} data-status=${t.status}>
        <header class="cycle-header">
          <span class="cycle-kind">${Ct[t.kind]}</span>
          ${void 0===e?Y:G`<span class="cycle-status">${e}</span>`}
          <span class="cycle-window">${this._window(t.start,t.end)}</span>
        </header>
        ${0===t.segments.length?G`<p class="empty">No zones are planned for this cycle.</p>`:this._renderLanes(t)}
      </section>
    `}_renderLanes(t){const{segments:e}=t,i=e.length*Jt,s=this._cursor(t);return G`
      <div class="lanes" style=${`--hic-lanes: ${e.length}`}>
        <ol class="labels">
          ${e.map(t=>G`
              <li class="label" data-status=${t.status}>
                ${this._statusGlyph(t.status)}
                <span class="zone">${t.name}</span>
                ${this._statusText(t.status)}
                <span class="times">${this._window(t.start,t.end)}</span>
              </li>
            `)}
        </ol>
        <svg
          class="bars"
          viewBox="0 0 ${Kt} ${i}"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          ${e.map((t,e)=>{const i=t.x0*Kt,r=e*Jt+5,o=Math.max(0,(t.x1-t.x0)*Kt);return J`
              <rect
                class="segment"
                data-zone=${t.zoneId}
                data-status=${t.status}
                x=${i}
                y=${r}
                width=${o}
                height=${18}
              ></rect>
              ${"running"===t.status?J`
                    <rect
                      class="progress"
                      data-zone=${t.zoneId}
                      x=${i}
                      y=${r}
                      width=${o}
                      height=${18}
                      style=${`transform: scaleX(${function(t,e){switch(t.status){case"running":{const i=t.x1-t.x0;return i>0?(Math.min(t.x1,Math.max(t.x0,e??t.x1))-t.x0)/i:0}case"completed":case"failed":return 1;default:return 0}}(t,s)})`}
                    ></rect>
                  `:Y}
            `})}
          ${void 0===s?Y:J`
              <line
                class="cursor"
                x1=${s*Kt}
                x2=${s*Kt}
                y1="0"
                y2=${i}
              ></line>
            `}
        </svg>
      </div>
    `}_window(t,e){return`${this._time(t)} – ${this._time(e)}`}_time(t){const e=new Date(t);if(Number.isNaN(e.getTime()))return"–";const i=this._hass?.locale;return i?((t,e)=>r(e).format(t))(e,i):e.toLocaleTimeString()}_ensureSubscribed(){this.isConnected&&void 0!==this._hass&&(this._listen(this._hass.connection),void 0===this._config||void 0!==this._unsubscribe||this._pending||void 0!==this._retry||this._subscribe(this._hass,this._config.entry_id))}async _subscribe(t,e){const i=++this._generation,s=()=>i!==this._generation;this._pending=!0;try{const i=await function(t,e,i){return t.connection.subscribeMessage(e,qt("ha_irrigation_controller/state_subscribe",i),{resubscribe:!1})}(t,t=>{s()||this._onView(t)},e);s()?i().catch(()=>{}):this._unsubscribe=i}catch(t){s()||(this._error=function(t){switch(t.code){case Ft:return"No irrigation controller was found. Set up the HA Irrigation Controller helper, or pass its entry_id in the card configuration.";case Wt:return"The irrigation controller is loading; the card will retry shortly.";default:return`Cannot read the irrigation controller (${t.code}${t.message?`: ${t.message}`:""}); retrying.`}}(function(t){if("object"==typeof t&&null!==t){const e=t;if(void 0!==e.code||void 0!==e.message)return{code:void 0===e.code?"unknown":String(e.code),message:"string"==typeof e.message?e.message:""}}return"number"==typeof t?{code:String(t),message:""}:t instanceof Error?{code:"unknown",message:t.message}:{code:"unknown",message:"string"==typeof t?t:""}}(t)),this._retry=setTimeout(()=>{this._retry=void 0,this._ensureSubscribed()},5e3))}finally{this._pending=!1}this._ensureSubscribed()}_listen(t){this._connection!==t&&(this._unlisten(),this._connection=t,t.addEventListener("disconnected",this._onDisconnected),t.addEventListener("ready",this._onReady))}_unlisten(){const t=this._connection;void 0!==t&&(t.removeEventListener("disconnected",this._onDisconnected),t.removeEventListener("ready",this._onReady),this._connection=void 0)}_onView(t){this._handshake={schema_version:t.schema_version,version:t.version};const e=function(t,e){const i=Date.parse(t);if(!Number.isNaN(i))return i-e}(t.generated_at,Date.now());void 0!==e&&(this._offsetMs=e),this._nowMs=gt(this._offsetMs),this._view=t,void 0!==this._error&&(this._error=void 0),this._syncTicker()}_syncTicker(){const t=this.isConnected&&void 0!==this._view?(e=this._view,"running"===e?.runs.current?.status?1e3:6e4):void 0;var e;t!==this._tickerPeriod&&(this._stopTicker(),void 0!==t&&(this._tickerPeriod=t,this._ticker=setInterval(this._tick,t)))}_stopTicker(){void 0!==this._ticker&&clearInterval(this._ticker),this._ticker=void 0,this._tickerPeriod=void 0}_refetch(){const t=this._hass;if(void 0===t||void 0===this._unsubscribe)return;const e=this._generation;(function(t,e){return t.callWS(qt("ha_irrigation_controller/state_get",e))})(t,this._config?.entry_id).then(t=>{e===this._generation&&void 0!==this._unsubscribe&&this._onView(t)}).catch(()=>{})}_closeSubscription(){void 0!==this._retry&&(clearTimeout(this._retry),this._retry=void 0),this._generation+=1;const t=this._unsubscribe;this._unsubscribe=void 0,void 0!==t&&t().catch(()=>{})}static get styles(){return h`
      :host {
        display: block;
        --hic-lane-height: ${Jt}px;
      }
      /* Slotted into ha-card, which gives .card-header its own header
         typography and padding; only the layout is ours. */
      .card-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
      }
      /* An h1 for the heading semantics ha-card's own header has; the
         slotted typography is inherited from .card-header, not the UA's. */
      .card-header .title {
        font: inherit;
        letter-spacing: inherit;
        margin: 0;
        min-width: 0;
      }
      /* The health chip: rem-sized so the 24 px header font does not scale it. */
      .health {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        flex: none;
        font-size: 0.875rem;
        font-weight: 400;
        line-height: normal;
        letter-spacing: normal;
        white-space: nowrap;
        color: var(--hic-secondary-text-color, var(--secondary-text-color));
        --mdc-icon-size: 18px;
      }
      .health ha-icon {
        display: flex;
        width: 18px;
        height: 18px;
        color: var(--hic-health-nominal-color, var(--hic-completed-color, var(--success-color, #43a047)));
      }
      .health[data-state="anomaly"],
      .health[data-state="anomaly"] ha-icon {
        color: var(--hic-health-anomaly-color, var(--hic-error-color, var(--error-color, #db4437)));
      }
      .health[data-state="anomaly"] {
        font-weight: 500;
      }
      .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        margin: -1px;
        padding: 0;
        overflow: hidden;
        clip: rect(0 0 0 0);
        clip-path: inset(50%);
        white-space: nowrap;
        border: 0;
      }
      .content {
        padding: 0 16px 16px;
        color: var(--hic-text-color, var(--primary-text-color));
      }

      /* --------------------------------------------------- anomaly banner */
      /* The loudest thing on the card, and the only filled block besides the
         history badges: the error colour as a bar and a tint, both read
         through hooks with HA theme-var fallbacks and never defined here. */
      .anomalies {
        margin: 0 0 16px;
        padding: 12px;
        border-radius: 8px;
        border-left: 4px solid var(--hic-health-anomaly-color, var(--hic-error-color, var(--error-color, #db4437)));
        background: var(--hic-health-anomaly-bg, color-mix(in srgb, var(--hic-health-anomaly-color, var(--hic-error-color, var(--error-color, #db4437))) 15%, transparent));
      }
      .anomalies-header {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 8px;
        line-height: 20px;
        font-weight: 500;
        color: var(--hic-health-anomaly-color, var(--hic-error-color, var(--error-color, #db4437)));
        --mdc-icon-size: 20px;
      }
      .anomalies-header ha-icon {
        display: flex;
        width: 20px;
        height: 20px;
        color: inherit;
      }
      .anomaly-list {
        list-style: none;
        margin: 0;
        padding: 0;
      }
      .anomaly {
        display: flex;
        align-items: flex-start;
        gap: 8px;
        min-height: ${24}px;
        line-height: ${24}px;
        font-size: 0.9em;
        --mdc-icon-size: 18px;
      }
      .anomaly ha-icon {
        display: flex;
        flex: none;
        /* Centred on the first 24 px line of a text that may wrap. */
        margin-top: 3px;
        width: 18px;
        height: 18px;
        color: var(--hic-health-anomaly-color, var(--hic-error-color, var(--error-color, #db4437)));
      }
      /* Wraps: on a narrow card the subject (which valve) must stay readable. */
      .anomaly-text {
        min-width: 0;
      }
      .anomaly-subject {
        color: var(--hic-secondary-text-color, var(--secondary-text-color));
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
        position: relative;
        display: flex;
        align-items: center;
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
      /* The terminal-status glyph beside the zone name: colour is not the
         only signal, and the glyph reads in the same hook as its bar. */
      .label ha-icon {
        display: flex;
        flex: none;
        width: 16px;
        height: 16px;
        --mdc-icon-size: 16px;
      }
      .label[data-status="completed"] ha-icon {
        color: var(--hic-completed-color, var(--success-color, #43a047));
      }
      .label[data-status="failed"] ha-icon {
        color: var(--hic-failed-color, var(--error-color, #db4437));
      }
      .label .times {
        flex: none;
      }
      .bars {
        display: block;
        width: 100%;
        height: calc(var(--hic-lanes, 1) * var(--hic-lane-height));
        border-radius: 4px;
        background: var(--hic-track-color, var(--divider-color, color-mix(in srgb, currentColor 12%, transparent)));
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
      @media (prefers-reduced-motion: reduce) {
        /* The fill steps once a second instead of gliding. */
        .progress {
          transition: none;
        }
      }
      .cursor {
        stroke: var(--hic-cursor-color, var(--primary-text-color, currentColor));
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
        border-top: 1px solid var(--hic-track-color, var(--divider-color, color-mix(in srgb, currentColor 12%, transparent)));
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
        color: var(--hic-history-empty-color, var(--hic-track-color, var(--divider-color, color-mix(in srgb, currentColor 12%, transparent))));
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
    `}}function ae(t){return"nominal"===t.state?0:68+24*t.items.length}t([ft()],ne.prototype,"_config",void 0),t([ft()],ne.prototype,"_view",void 0),t([ft()],ne.prototype,"_error",void 0),t([ft()],ne.prototype,"_nowMs",void 0),customElements.get(Zt)||customElements.define(Zt,ne),window.customCards=window.customCards??[],window.customCards.some(t=>t.type===Zt)||window.customCards.push({type:Zt,name:"HA Irrigation Timeline Card",description:"Today's irrigation plan: each cycle's zones as a proportional timeline with planned times, live progress and outcomes, the last seven days at a glance, and the controller's health."}),console.info(`%c ${Zt.toUpperCase()} %c v0.1.0`,"color: white; background: #2e7d32; font-weight: 700;","");export{Qt as ANOMALY_HEADER_PX,te as ANOMALY_ROW_PX,Yt as HISTORY_HEADER_PX,Xt as HISTORY_ROW_PX,ne as HaIrrigationTimelineCard,Jt as LANE_HEIGHT,Gt as RETRY_DELAY_MS};

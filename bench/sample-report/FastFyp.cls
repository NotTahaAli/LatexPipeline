\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{FastFyp}[2024/01/01 Synthetic final-year-project report class]
\DeclareOption*{\PassOptionsToClass{\CurrentOption}{report}}
\ProcessOptions\relax
\LoadClass[12pt,a4paper,twoside]{report}

\RequirePackage[a4paper,top=2.5cm,bottom=2.5cm,inner=3.2cm,outer=2.5cm]{geometry}
\RequirePackage[T1]{fontenc}
\RequirePackage[utf8]{inputenc}
\RequirePackage{lmodern}
\RequirePackage{graphicx}
\RequirePackage{amsmath,amssymb,amsthm}
\RequirePackage{booktabs,longtable,float}
\RequirePackage[dvipsnames,table]{xcolor}
\RequirePackage{tikz}
\usetikzlibrary{arrows.meta,positioning,shapes.geometric,calc,fit,backgrounds,matrix,decorations.pathmorphing,patterns,3d}
\RequirePackage{pgfplots}
\usepgfplotslibrary{groupplots,fillbetween,colormaps}
\pgfplotsset{compat=1.18}
\RequirePackage{listings}
\RequirePackage[font=small,labelfont=bf]{caption}
\RequirePackage{subcaption}
\RequirePackage{setspace}
\RequirePackage{fancyhdr}
\RequirePackage[explicit]{titlesec}
\RequirePackage{enumitem}
\RequirePackage{siunitx}
\RequirePackage[numbers,sort&compress]{natbib}
\RequirePackage{chapterbib}
\RequirePackage[colorlinks=true,linkcolor=NavyBlue,citecolor=ForestGreen,urlcolor=BrickRed]{hyperref}
\RequirePackage[capitalise,nameinlink]{cleveref}

\setlength{\headheight}{15pt}
\setstretch{1.15}
\setlength{\parindent}{1.2em}
\setlist{itemsep=2pt,topsep=4pt}
\definecolor{codebg}{gray}{0.96}
\lstset{basicstyle=\ttfamily\footnotesize,backgroundcolor=\color{codebg},frame=single,
  numbers=left,numberstyle=\tiny,breaklines=true,keywordstyle=\color{blue}\bfseries,
  commentstyle=\color{gray},columns=fullflexible}
\theoremstyle{plain}
\newtheorem{theorem}{Theorem}[chapter]
\newtheorem{lemma}[theorem]{Lemma}
\theoremstyle{definition}
\newtheorem{definition}[theorem]{Definition}

\titleformat{\chapter}[display]{\normalfont\huge\bfseries\color{NavyBlue}}{\chaptertitlename\ \thechapter}{12pt}{\Huge #1}
\titlespacing*{\chapter}{0pt}{0pt}{30pt}
\pagestyle{fancy}
\fancyhf{}
\fancyhead[LE,RO]{\thepage}
\fancyhead[RE]{\nouppercase{\leftmark}}
\fancyhead[LO]{\nouppercase{\rightmark}}
\renewcommand{\headrulewidth}{0.4pt}
\fancypagestyle{plain}{\fancyhf{}\fancyfoot[C]{\thepage}\renewcommand{\headrulewidth}{0pt}}

% Title-page metadata
\newcommand{\university}[1]{\gdef\@university{#1}}
\newcommand{\department}[1]{\gdef\@department{#1}}
\newcommand{\degree}[1]{\gdef\@degree{#1}}
\newcommand{\supervisor}[1]{\gdef\@supervisor{#1}}
\newcommand{\studentid}[1]{\gdef\@studentid{#1}}
\university{}\department{}\degree{}\supervisor{}\studentid{}

\newcommand{\maketitlepage}{%
  \begin{titlepage}\centering
    {\Large\scshape\@university\par}\vspace{4pt}
    {\large\@department\par}
    \vspace{3cm}
    {\Huge\bfseries\color{NavyBlue}\@title\par}
    \vspace{1.5cm}
    {\Large\@author\par}{\small Student ID: \@studentid\par}
    \vfill
    {\large A final year project report submitted in partial fulfilment of the requirements for the degree of \@degree\par}
    \vspace{1cm}
    {\large Supervisor: \@supervisor\par}
    \vspace{1cm}{\large\@date\par}
  \end{titlepage}}

\newenvironment{declaration}
  {\chapter*{Declaration}\addcontentsline{toc}{chapter}{Declaration}\thispagestyle{plain}}
  {\vspace{2cm}\noindent Signed: \rule{6cm}{0.4pt}\hfill Date: \rule{3cm}{0.4pt}\clearpage}
\newenvironment{acknowledgements}
  {\chapter*{Acknowledgements}\addcontentsline{toc}{chapter}{Acknowledgements}}
  {\clearpage}

% \fypinput{<aux-name>}{<path>}: like \input, but writes the unit's citations
% to its own <aux-name>.aux (flat name, so latexmk -outdir works) and prints
% its bibliography from <aux-name>.bbl. Built on chapterbib's cbunit.
\makeatletter
% \fypreadaux (not \@input) hides the unit aux from bibtex run on main.aux.
\newcommand{\fypreadaux}[1]{\@input{#1}}
\newcommand{\fypinput}[2]{%
  \cbunit\xdef\@currentipfile{#1}%
  \if@filesw
    \immediate\write\@mainaux{\string\fypreadaux{#1.aux}}%
    \immediate\openout\@partaux=#1.aux
    \immediate\write\@partaux{\relax}%
  \fi
  \let\@auxout\@partaux
  \input{#2}%
  \clearpage
  \if@filesw\immediate\closeout\@partaux\fi
  \let\@auxout\@mainaux
  \global\let\@currentipfile\@savedjobname
  \endcbunit}
\makeatother
\newcommand{\unitbib}[1]{%
  \begingroup\bibliographystyle{plainnat}%
  \renewcommand{\bibsection}{\section*{References for this chapter}}%
  \small\bibliography{#1}\endgroup}
\newcommand{\frontmatter}{\pagenumbering{roman}}
\newcommand{\mainmatter}{\clearpage\pagenumbering{arabic}}
\newcommand{\backmatter}{\clearpage}
\endinput

!> Main demo program: fpm run demo
program demo_main
    use demo
    implicit none
    real(wp), allocatable :: x(:)
#ifdef DEMO_GREETING
    print '(a)', "Hello! (the 'greeting' feature is on)"
#endif
    x = linspace(0.0_wp, 1.0_wp, 11)
    print '(a)', "mean of linspace(0,1,11)  = "//to_string(mean(x))
    print '(a)', "variance                  = "//to_string(variance(x))
    print '(a)', "clamp(1.7, 0, 1)          = "//to_string(clamp(1.7_wp, 0.0_wp, 1.0_wp))
    print '(a)', "minimum of (x-2)**2 near  = "//to_string(golden_minimum())
    print '(a,i0)', "broken_answer()           = ", broken_answer()
    print '(a,i0)', "OpenMP threads available  = ", max_threads()
end program demo_main
